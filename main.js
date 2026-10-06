'use strict';
const {Plugin, ItemView, Notice, PluginSettingTab, Setting, requestUrl} = require('obsidian');
const {spawn} = require('child_process');
const fs = require('fs');
const path = require('path');
const TYPE = 'finanzas-local';
const BASE = 'http://127.0.0.1:5028';
const SECTIONS = {inicio: '/', deudas: '/deudas', ingresos: '/ingresos', ajustes: '/configuracion'};

class FinanceView extends ItemView {
    constructor(leaf, plugin) { super(leaf); this.plugin = plugin; this.section = 'inicio'; }
    getViewType() { return TYPE; }
    getDisplayText() { return 'Finanzas'; }
    getIcon() { return 'wallet'; }
    getState() { return {section: this.section}; }
    async setState(state, result) {
        this.section = SECTIONS[state.section] ? state.section : 'inicio';
        if (this.webview) await this.navigate(this.section);
        await super.setState(state, result);
    }
    async onOpen() {
        this.contentEl.empty(); this.contentEl.addClass('finanzas-view');
        const toolbar = this.contentEl.createDiv({cls: 'finanzas-toolbar'});
        this.buttons = {};
        for (const section of Object.keys(SECTIONS)) {
            const button = toolbar.createEl('button', {text: section[0].toUpperCase()+section.slice(1)});
            this.registerDomEvent(button, 'click', () => this.navigate(section));
            this.buttons[section] = button;
        }
        this.status = this.contentEl.createDiv({cls: 'finanzas-service-status', attr: {role:'status'}});
        this.webview = document.createElement('webview');
        this.webview.className = 'finanzas-webview';
        // Use Obsidian's persistent browser session: it cleans unknown vault partitions on startup.
        this.webview.setAttribute('webpreferences', 'contextIsolation=yes,nodeIntegration=no');
        this.registerDomEvent(this.webview, 'dom-ready', () => {
            this.status.textContent = '';
            this.applyTheme();
        });
        this.registerDomEvent(this.webview, 'did-fail-load', event => {
            if (event.isMainFrame && event.errorCode !== -3) this.status.textContent = 'No se pudo cargar Finanzas. Pulsa Inicio para reintentar.';
        });
        this.registerDomEvent(this.webview, 'did-navigate', event => {
            const current = new URL(event.url);
            this.section = Object.keys(SECTIONS).find(key => SECTIONS[key] === current.pathname) || this.section;
            this.highlight();
        });
        this.registerDomEvent(this.webview, 'will-navigate', event => {
            if (new URL(event.url).origin !== BASE) event.preventDefault();
        });
        this.contentEl.appendChild(this.webview);
        this.registerEvent(this.app.workspace.on('css-change', () => this.applyTheme()));
        await this.navigate(this.section);
    }
    highlight() {
        for (const [key, button] of Object.entries(this.buttons || {})) button.classList.toggle('is-active', key === this.section);
    }
    async navigate(section) {
        this.section = SECTIONS[section] ? section : 'inicio'; this.highlight();
        this.status.textContent = 'Conectando con Finanzas…';
        try {
            await this.plugin.ensureServer();
            if (!this.webview?.isConnected) return;
            this.webview.setAttribute('src', BASE + SECTIONS[this.section]);
        } catch (error) {
            this.status.textContent = error.message;
        }
    }
    async applyTheme() {
        if (!this.webview?.isConnected) return;
        const style = getComputedStyle(document.body);
        const mapping = {'--bg':'--background-primary','--surface':'--background-primary',
            '--sidebar':'--background-secondary','--hover':'--background-modifier-hover',
            '--selected':'--background-modifier-active-hover','--ink':'--text-normal',
            '--muted':'--text-muted','--faint':'--text-faint','--line':'--background-modifier-border',
            '--line-strong':'--background-modifier-border-hover','--focus':'--interactive-accent',
            '--positive':'--text-success','--danger':'--text-error','--warning':'--text-warning',
            '--font':'--font-interface'};
        const values = Object.fromEntries(Object.entries(mapping).map(([key, source]) => [key,style.getPropertyValue(source).trim()]).filter(([,value])=>value));
        try {
            await this.webview.executeJavaScript(`(() => {
                const values = ${JSON.stringify(values)};
                Object.entries(values).forEach(([key,value]) => document.documentElement.style.setProperty(key,value));
                for (const key of ['positive','danger','warning']) document.documentElement.style.setProperty('--'+key+'-bg', 'color-mix(in srgb, var(--'+key+') 12%, var(--surface))');
                document.documentElement.style.colorScheme = ${JSON.stringify(document.body.classList.contains('theme-dark')?'dark':'light')};
                let style = document.getElementById('obsidian-finanzas-theme');
                if (!style) { style = document.createElement('style'); style.id = 'obsidian-finanzas-theme'; document.head.appendChild(style); }
                style.textContent = '.sidebar,.mobile-nav,.workspace>footer{display:none!important}.app-shell{display:block!important}.workspace{margin-left:0!important;min-height:100vh}.page-shell{max-width:none!important;padding:20px!important}body{padding-bottom:0!important}.home-hero{margin-bottom:20px!important}.login-card{background:var(--surface)!important;color:var(--ink)!important}';
            })()`);
        } catch (_) { /* Loading or closing the view: dom-ready retries. */ }
    }
    async onClose() { this.webview = null; }
}

class FinanceSettings extends PluginSettingTab {
    constructor(app, plugin) { super(app, plugin); this.plugin = plugin; }
    display() {
        this.containerEl.empty(); this.containerEl.createEl('h2',{text:'Finanzas'});
        new Setting(this.containerEl).setName('Python').setDesc('Ejecutable de Python que inicia Flask en este equipo.')
            .addText(text => text.setValue(this.plugin.settings.python).onChange(async value => {
                this.plugin.settings.python = value.trim(); await this.plugin.saveData(this.plugin.settings);
            }));
        new Setting(this.containerEl).setName('Iniciar el servidor al abrir la bóveda')
            .setDesc('Ejecuta el servidor Python local. No instala ni descarga dependencias.')
            .addToggle(toggle => toggle.setValue(this.plugin.settings.autoStart).onChange(async value => {
                this.plugin.settings.autoStart = value; await this.plugin.saveData(this.plugin.settings);
            }));
        new Setting(this.containerEl).setName('Abrir Finanzas').setDesc('Aplicación integrada en una pestaña de Obsidian.')
            .addButton(button => button.setButtonText('Abrir').onClick(()=>this.plugin.open('inicio')));
    }
}

module.exports = class FinancePlugin extends Plugin {
    async onload() {
        this.settings = Object.assign({python:process.platform === 'win32' ? 'py' : 'python3', autoStart:false}, await this.loadData());
        this.registerView(TYPE, leaf => new FinanceView(leaf,this));
        this.addRibbonIcon('wallet', 'Abrir Finanzas', () => this.open('inicio'));
        this.addCommand({id:'abrir',name:'Abrir Finanzas',callback:()=>this.open('inicio')});
        this.addCommand({id:'deudas',name:'Abrir deudas',callback:()=>this.open('deudas')});
        this.registerObsidianProtocolHandler('finanzas', params => this.open(params.section || 'inicio'));
        this.addSettingTab(new FinanceSettings(this.app,this));
        this.app.workspace.onLayoutReady(() => {
            if (this.settings.autoStart) this.ensureServer().catch(error=>new Notice(error.message));
        });
    }
    async open(section) {
        try {
            const leaf = this.app.workspace.getLeavesOfType(TYPE)[0] || this.app.workspace.getLeaf('tab');
            await leaf.setViewState({type:TYPE,active:true,state:{section:SECTIONS[section]?section:'inicio'}});
            this.app.workspace.revealLeaf(leaf);
        } catch (error) { new Notice('No se pudo abrir Finanzas: '+error.message); }
    }
    async checkServer() {
        try {
            const response = await requestUrl({url:BASE+'/api/health',throw:false});
            if (response.status === 200 && response.json.service === '27-finanzas') return true;
            if (response.status === 200) throw new Error('El puerto 5028 está ocupado por otra aplicación.');
        } catch (error) {
            if (error.message?.includes('ocupado')) throw error;
        }
        return false;
    }
    async ensureServer() {
        if (this.starting) return this.starting;
        this.starting = this.startServer();
        try { return await this.starting; } finally { this.starting = null; }
    }
    async startServer() {
        if (await this.checkServer()) return;
        const root = path.join(this.app.vault.adapter.getBasePath(),'.finanzas');
        if (!fs.existsSync(path.join(root,'run.py'))) throw new Error('No se encontró el sistema en '+root);
        let failure = '';
        const python = this.settings.python || 'py';
        const args = python.toLowerCase() === 'py' ? ['-3',path.join(root,'run.py')] : [path.join(root,'run.py')];
        const child = spawn(python,args,{cwd:root,windowsHide:true,detached:true,stdio:'ignore'});
        child.on('error',error=>{failure=error.message;}); child.unref();
        for (let attempt=0;attempt<40;attempt++) {
            if (failure) throw new Error('No se pudo iniciar Python. Revisa Ajustes → Finanzas. '+failure);
            await new Promise(resolve=>setTimeout(resolve,250));
            if (await this.checkServer()) return;
        }
        throw new Error('Flask no respondió. Revisa .finanzas/logs y pulsa Inicio para reintentar.');
    }
};
