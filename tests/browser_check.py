"""Explicit browser checks with synthetic data for the minimal debt experience."""

def main():
    import os, sys, tempfile, threading, logging
    from pathlib import Path
    from datetime import date
    from werkzeug.serving import make_server, WSGIRequestHandler
    from playwright.sync_api import sync_playwright
    root=Path(__file__).resolve().parents[1];(root/'.cache').mkdir(exist_ok=True);temp=tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    os.environ['FINANZAS_DB_PATH']=str(Path(temp.name)/'bootstrap.db');sys.path.insert(0,str(root))
    os.environ["QUINCENAL_INCOME_CENTS"]="1000000"
    import app as f
    f._local_today=lambda timezone:date(2026,10,5)
    a=f.create_app({'TESTING':True,'AUTH_MODE':'none','SECRET_KEY':'browser-only','DATABASE_PATH':Path(temp.name)/'test.db','LOGS_DIR':Path(temp.name)/'logs','TATTOO_UPLOAD_DIR':Path(temp.name)/'uploads'})
    c=a.test_client()
    with c.session_transaction() as s:s['csrf_token']='test'
    def post(path,**fields):return c.post(path,data={'csrf_token':'test',**fields})
    post('/deudas',debt_type='single',name='Persona',amount='120',due_on='2026-10-15')
    post('/deudas',debt_type='installments',creditor='Moto',amount='1200',min_payment='500',schedule_type='monthly',due_on='2026-10-15')
    post('/deudas',debt_type='recurring',title='Internet',amount='500',frequency='monthly',starts_on='2026-10-05',month_day='5',category='Deudas')
    class Quiet(WSGIRequestHandler):
     def log_request(self,*args):pass
    server=make_server('127.0.0.1',0,a,threaded=True,request_handler=Quiet);threading.Thread(target=server.serve_forever,daemon=True).start();base=f'http://127.0.0.1:{server.server_port}'
    with sync_playwright() as p:
     browser=p.chromium.launch(channel='msedge',headless=True);page=browser.new_page(viewport={'width':1440,'height':1000});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
     page.goto(base);page.wait_for_load_state('networkidle');assert page.locator('.month-day').count()==35
     page.locator('[data-open="payment-s-1"]').first.click();assert page.locator('#payment-s-1').is_visible();page.keyboard.press('Escape')
     handle=page.locator('[data-payment-card]').filter(has=page.locator('[data-open="payment-s-1"]')).locator('.payment-drag-handle')
     b=handle.bounding_box();target=page.locator('[data-drop-date="2026-10-16"]').bounding_box()
     page.mouse.move(b['x']+b['width']/2,b['y']+b['height']/2);page.mouse.down();page.mouse.move(target['x']+target['width']/2,target['y']+50,steps=15);page.mouse.up();page.wait_for_timeout(1200)
     with f._db_connection(a) as db:assert db.execute('SELECT todo_due_on FROM debts WHERE id=1').fetchone()[0]=='2026-10-16'
     handle.focus();page.keyboard.press('Enter');assert page.locator('#payment-s-1').is_visible();page.keyboard.press('Escape')
     page.screenshot(path=str(root/'.cache/minimal-home-desktop.png'),full_page=True)
     page.goto(base+'/deudas');page.locator('#deuda-1 [data-open]').click();assert page.locator('#todo-1 input:visible').count()==3;page.keyboard.press('Escape')
     page.locator('#deuda-1 .todo-check').click();page.wait_for_timeout(800);assert page.locator('#deuda-1').count()==0;assert page.get_by_role('button',name='Deshacer',exact=True).count()==0;assert 'Completadas' not in page.locator('body').inner_text()
     page.goto(base);assert page.locator('#payment-s-1').count()==0;assert page.locator('.minimal-overdue').count()==0
     post('/deudas',debt_type='single',name='Pendiente movil',amount='50',due_on='2026-10-16')
     page.goto(base+'/deudas')
     page.locator('#deuda-2 [data-open]').click();page.locator('#installment-2 input[name="amount"]').fill('250');page.locator('#installment-2 button.primary-button').click();page.wait_for_load_state('networkidle');assert page.locator('#deuda-2 progress').get_attribute('value')=='21'
     page.screenshot(path=str(root/'.cache/minimal-debts-desktop.png'),full_page=True)
     page.set_viewport_size({'width':390,'height':844});page.goto(base);page.wait_for_load_state('networkidle');assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
     assert page.locator('.month-weekday:visible').count()==7
     page.locator('[data-drop-date="2026-10-16"] .minimal-day-button').click();assert page.locator('#day-2026-10-16').is_visible();page.locator('#day-2026-10-16 [data-open]').click();assert page.locator('dialog[id^=payment-s-][open]').is_visible();page.keyboard.press('Escape')
     page.screenshot(path=str(root/'.cache/minimal-home-mobile.png'),full_page=True)
     page.goto(base+'/deudas');page.screenshot(path=str(root/'.cache/minimal-debts-mobile.png'),full_page=True)
     page.goto(base+'/deudas');page.locator('[data-open="new-debt"]').click()
     form=page.locator('#new-debt form');form.locator('select[name="debt_type"]').select_option('installments')
     assert form.locator('input:visible').count()==4
     group=form.locator('[data-debt-type="installments"]');group.locator('[name="creditor"]').fill('Nueva persona');group.locator('[name="amount"]').fill('120');group.locator('[name="min_payment"]').fill('50');group.locator('[name="schedule_type"]').select_option('monthly');group.locator('[name="due_on"]').fill('2026-11-15');form.locator('button.primary-button').click();page.wait_for_load_state('networkidle')
     with f._db_connection(a) as db:
      new_id=db.execute("SELECT id FROM debts WHERE creditor='Nueva persona'").fetchone()[0]
      assert [r['amount_cents'] for r in db.execute('SELECT amount_cents FROM scheduled_debt_payments WHERE debt_id=? ORDER BY due_on',(new_id,))]==[5000,5000,2000]
     page.goto(base+'/deudas#deuda-recurrente-1');edit=page.locator('#recurring-1');assert edit.is_visible()
     edit.locator('[name="frequency"]').select_option('weekly');assert edit.locator('input:visible').count()==3
     edit.locator('input[type=date][name="starts_on"]').fill('2026-10-07');edit.locator('button.primary-button').click();page.wait_for_load_state('networkidle')
     with f._db_connection(a) as db:assert db.execute('SELECT weekday FROM recurring_commitments WHERE id=1').fetchone()[0]==2
     page.set_viewport_size({'width':1440,'height':1000});page.goto(base);page.wait_for_load_state('networkidle')
     recurring_handle=page.locator('[data-payment-card]').filter(has=page.locator('[data-open="payment-r-1-2026-10-07"]')).locator('.payment-drag-handle')
     b=recurring_handle.bounding_box();target=page.locator('[data-drop-date="2026-10-08"]').bounding_box()
     page.mouse.move(b['x']+b['width']/2,b['y']+b['height']/2);page.mouse.down();page.mouse.move(target['x']+target['width']/2,target['y']+50,steps=15);page.mouse.up();page.wait_for_timeout(1200)
     with f._db_connection(a) as db:assert db.execute("SELECT moved_to FROM commitment_exceptions WHERE commitment_id=1 AND occurrence_on='2026-10-07'").fetchone()[0]=='2026-10-08'
     page.locator('[data-open="payment-r-1-2026-10-07"]').first.click()
     page.locator('#payment-r-1-2026-10-07 form[data-complete] button').click();page.wait_for_timeout(800)
     assert page.locator('#payment-r-1-2026-10-07').count()==0
     assert page.locator('#payment-r-1-2026-10-14').count()==1
     assert page.get_by_role('button',name='Deshacer',exact=True).count()==0
     page.set_viewport_size({'width':390,'height':844})
     post('/deudas',debt_type='single',name='Exceso de prueba',amount='11000',due_on='2026-10-05')
     page.goto(base);page.wait_for_load_state('networkidle');assert page.locator('.over-income').count()==1
     measure = r"""el => {
     const canvas=document.createElement('canvas');canvas.width=canvas.height=1;const ctx=canvas.getContext('2d');
     const rgba=value=>{ctx.clearRect(0,0,1,1);ctx.fillStyle=value;ctx.fillRect(0,0,1,1);return [...ctx.getImageData(0,0,1,1).data];};
     const lum=color=>color.slice(0,3).map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4;}).reduce((sum,v,i)=>sum+v*[.2126,.7152,.0722][i],0);
     let parent=el,bg;while(parent){const value=rgba(getComputedStyle(parent).backgroundColor);if(value[3]>0){bg=value;break;}parent=parent.parentElement;}
     bg=bg||[255,255,255,255];const ink=rgba(getComputedStyle(el).color);return (Math.max(lum(ink),lum(bg))+.05)/(Math.min(lum(ink),lum(bg))+.05);
     }"""
     palettes=[{'--bg':'#fafafa','--surface':'#fafafa','--ink':'#222','--muted':'#555','--line':'#ddd','--line-strong':'#888','--hover':'#eee','--danger':'#ff5263'},
               {'--bg':'#1e1e1e','--surface':'#1e1e1e','--ink':'#dadada','--muted':'#aaa','--line':'#333','--line-strong':'#777','--hover':'#292929','--danger':'#ff5263'}]
     ratios=[]
     for i,palette in enumerate(palettes):
      page.goto(base);page.wait_for_load_state('networkidle');page.evaluate('(values)=>Object.entries(values).forEach(([key,value])=>document.documentElement.style.setProperty(key,value))',palette)
      page.add_style_tag(content='.sidebar,.mobile-nav,.workspace>footer{display:none!important}.app-shell{display:block!important}.workspace{margin-left:0!important}.page-shell{padding:20px!important}body{padding-bottom:0!important}')
      page.wait_for_timeout(300)
      for el in page.locator('button:visible,a.secondary-button:visible,.over-income strong,.over-income span').all():
       ratio=el.evaluate(measure);ratios.append(ratio)
       if ratio<4.5:print('Low contrast',i,round(ratio,2),el.get_attribute('class'),el.evaluate('(el)=>({ink:getComputedStyle(el).color,bg:getComputedStyle(el).backgroundColor})'))
      page.screenshot(path=str(root/f'.cache/minimal-obsidian-{i}.png'),full_page=True)
      assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
     assert min(ratios)>=4.5,min(ratios)
     print('Obsidian theme simulation light/dark: minimum text contrast',round(min(ratios),2))
     assert not errors,errors
     print('Browser OK: desktop/mobile, dialogs, completion/removal, payment/progress, drag, no overflow, no JS errors')
     browser.close()
    server.shutdown();logging.shutdown();temp.cleanup()

if __name__ == '__main__':
    main()
