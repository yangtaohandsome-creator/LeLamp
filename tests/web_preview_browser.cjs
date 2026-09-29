// Run with Playwright available on NODE_PATH and the hardware-free web demo running.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/snap/bin/chromium',headless:true,args:['--no-sandbox']});
 try {
  const p=await browser.newPage();const errors=[];p.on('pageerror',e=>errors.push(e.message));
  await p.goto(process.env.WEB_DEMO_URL||'http://127.0.0.1:18795');
  await p.getByRole('button',{name:'控制',exact:true}).click();
  await p.getByRole('button',{name:'开启默认照明',exact:true}).click();
  let count=0, busy=false;
  await p.route('**/vision/preview',async route=>{
   if(busy || ++count%3===0){await route.fulfill({status:429});return;}
   const response=await route.fetch();const body=await response.body();
   // Model age plus LAN delay exceeded the former 500ms expiry between frames.
   await new Promise(r=>setTimeout(r,180));
   await route.fulfill({response,body});
  });
  await p.locator('#preview-toggle').click();
  await p.waitForFunction(()=>!document.querySelector('#vision-preview').hidden);
  await p.evaluate(()=>{
   window.previewFlashes=0;
   window.previewObserver=new MutationObserver(()=>{if(document.querySelector('#vision-preview').hidden)window.previewFlashes++});
   window.previewObserver.observe(document.querySelector('#vision-preview'),{attributes:true,attributeFilter:['hidden']});
  });
  await p.waitForTimeout(6500);
  assert.equal(await p.evaluate(()=>window.previewFlashes),0,'normal latency/busy must not blank the canvas');
  busy=true;
  await p.waitForFunction(()=>document.querySelector('#preview-status').textContent.includes('保留最后一帧'));
  assert.equal(await p.locator('#vision-preview').evaluate(c=>c.hidden),false);
  await p.waitForFunction(()=>document.querySelector('#vision-preview').hidden,{timeout:4500});
  busy=false;
  await p.waitForFunction(()=>!document.querySelector('#vision-preview').hidden);
  await p.locator('#preview-toggle').click();
  assert.equal(await p.locator('#vision-preview').evaluate(c=>c.hidden),true);
  assert.deepEqual(errors,[]);
  console.log('PASS: delayed frames, intermittent 429, stale marking, bounded retention, recovery, close');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
