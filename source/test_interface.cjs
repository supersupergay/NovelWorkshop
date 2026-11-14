// Runs the actual UI script with a minimal DOM adapter and the actual local HTTP backend.
// Checks event wiring and state transitions; does not validate browser rendering.
const fs=require('fs'), vm=require('vm'), cp=require('child_process'), assert=require('assert'), os=require('os'), path=require('path');
const root=fs.mkdtempSync(path.join(os.tmpdir(),'novel-ui-'));
const proc=cp.spawn(process.env.PYTHON || 'python3',[path.join(__dirname,'novel.py'),'--no-browser','--data-dir',path.join(root,'data')]);
const elements=new Map();
class Element {
 constructor(tag){this.tagName=tag;this.value='';this.textContent='';this.children=[];this.dataset={};this.checked=false;this.disabled=false;this.open=false;this.style={};this.offsetTop=0;this.clientWidth=600;this.clientHeight=400;this.scrollTop=0;}
 set value(v){this._value=String(v);} get value(){return this._value;}
 append(...items){this.children.push(...items);}
 replaceChildren(...items){this.children=items;}
 set innerHTML(html){this.children=parse(html);}
 get options(){return this.children;}
 querySelectorAll(selector){let result=[];const visit=e=>{if(selector==='button'&&e.tagName==='button')result.push(e);if(selector==='input:checked'&&e.tagName==='input'&&e.checked)result.push(e);for(const c of e.children||[])visit(c);};for(const c of this.children)visit(c);return result;}
 setAttribute(name,value){this[name]=value;} focus(){} setSelectionRange(start,end){this.selectionStart=start;this.selectionEnd=end;} scrollIntoView(){} showModal(){this.open=true;} close(){this.open=false;this.style={};this.offsetTop=0;this.clientWidth=600;this.clientHeight=400;this.scrollTop=0;} remove(){} click(){if(!this.disabled)return this.onclick?.();}
}
function parse(html){let out=[];for(const m of html.matchAll(/<(input|textarea|select|button|div|p|span|a|details|dialog|label)[^>]*\bid="([^"]+)"[^>]*>/g)){const e=new Element(m[1]);e.id=m[2];e.open=/\sopen(?:\s|>)/.test(m[0]);elements.set(e.id,e);out.push(e);if(e.tagName==='select'){const o=new Element('option');e.children.push(o);}}return out;}
const html=fs.readFileSync(path.join(__dirname,'interface.html'),'utf8');parse(html);
const document={documentElement:new Element("html"),getElementById:id=>elements.get(id)||null,createElement:tag=>new Element(tag),querySelectorAll:()=>[...elements.values()].filter(e=>['textarea','input'].includes(e.tagName)),body:new Element('body')};
let url='', modelServer=null, modelCalls=0, reviewCalls=0, reviewMode='retry', readerFail=false, allowConfirm=true, confirmations=[], routes=[], paints=0;
const intervals=[];
const ctx=vm.createContext({document,localStorage:{getItem:()=>null,setItem:()=>{}},console,URL,URLSearchParams,Blob,setTimeout,clearTimeout,setInterval:(fn,ms)=>{const id=setInterval(fn,ms);intervals.push(id);return id;},clearInterval,requestAnimationFrame:fn=>setTimeout(()=>{paints++;fn();},0),getComputedStyle:()=>({}),confirm:message=>{confirmations.push(message);return allowConfirm;},window:{addEventListener:()=>{}},fetch:async(route,config={})=>{routes.push({route,body:elements.get("body").value,paints});return fetch(new URL(route,url),{...config,headers:{...config.headers,Origin:new URL(url).origin}});}});
function run(code){return vm.runInContext(code,ctx);}
async function wait(){for(let i=0;i<300;i++){if(!run('busy'))return;await new Promise(r=>setTimeout(r,10));}throw Error('UI operation timed out');}
async function click(id){const e=elements.get(id);assert(!e.disabled,id+' disabled');await e.onclick();await wait();assert.notEqual(elements.get('status').dataset.tone,'error',elements.get('status').textContent);}
(async()=>{
 url=await new Promise((resolve,reject)=>{proc.stdout.on('data',chunk=>{const match=String(chunk).match(/http:\/\/127\.0\.0\.1:\d+\/[^\s]+/);if(match)resolve(match[0]);});proc.on('error',reject);proc.on('exit',code=>reject(Error('server exited '+code)));});
 run(html.split('<script>')[1].split('</script>')[0]);await wait();
 assert.equal(elements.get('config').open,false);
 assert.equal(elements.get('new-project-dialog').open,false);
 assert.equal(elements.get('comment').disabled,true);
 assert.equal(elements.get('commentary-reply-area').hidden,true);
 assert.equal(elements.get('auto-review').checked,true);
 assert(html.indexOf('id="rules"')<html.indexOf('id="style"'));
 assert(html.indexOf('id="style"')<html.indexOf('id="review_requirements"'));
 assert.equal(run('state.projects.length'),1);assert(!html.includes('内置演示'));
 elements.get('title').value='初章';elements.get('pacing').value='中段放慢，结尾收住';await click('save-card');
 elements.get('review_requirements').value='注意重复解释';await click('save-review-requirements');
 elements.get('body').value='一段手工正文。';await click('manual');
 const directory=path.join(root,'中文目录 with spaces');elements.get('save-directory').value=directory;await click('apply-directory');
 await click('project-save');assert(fs.existsSync(path.join(directory,'我的小说.novelproj')));
 elements.get('body').value='尚未提交的正文';
 await click('project-rename');assert(elements.get('rename-project-dialog').open);
 elements.get('rename-project-name').value='更名后的小说';await click('confirm-rename');
 assert.equal(run('state.project_name'),'更名后的小说');assert.equal(elements.get('body').value,'尚未提交的正文');
 assert(fs.existsSync(path.join(directory,'更名后的小说.novelproj')));assert(!fs.existsSync(path.join(directory,'我的小说.novelproj')));
 run('autosaveDue.set(project,0); busy=true');await run('autosaveTick()');assert.equal(fs.readdirSync(directory).filter(n=>n.includes('自动保存')).length,0);
 run('busy=false');await run('autosaveTick()');
 const auto=fs.readdirSync(directory).find(n=>n.endsWith('_自动保存.novelproj'));assert(auto);assert.equal(JSON.parse(fs.readFileSync(path.join(directory,auto))).workspace.body,'尚未提交的正文');
 assert.equal(elements.get('body').value,'尚未提交的正文');
 await elements.get('theme-toggle').onclick();assert.equal(document.documentElement.dataset.theme,'night');
 await new Promise(r=>setTimeout(r,30));await elements.get('theme-toggle').onclick();assert.equal(document.documentElement.dataset.theme,'day');
 elements.get('export-format').value='rtf';elements.get('export-format').onchange();assert(elements.get('export').href.includes('format=rtf'));
 elements.get('body').value='甲“引文”。尾声。';run('draft.report={issues:[{draft_quote:"引文",source_quote:"引文",source_id:"DRAFT",draft_verified:true,source_verified:true,reason:"问题",suggestion:"修改",severity:"低",kind:"节奏"}]};paintReview()');
 const issue=elements.get('issues').children.find(e=>e.tagName==='article');assert.equal(issue.children.filter(e=>e.className==='quote').length,1);
 run('busy=true');issue.querySelectorAll('button')[0].onclick();assert.equal(elements.get('body').value.slice(elements.get('body').selectionStart,elements.get('body').selectionEnd),'引文');run('busy=false');
 assert.deepEqual(Array.from(run('quoteRanges("😀甲\\n  乙。", "甲乙。")')[0]),[2,8]);
 assert.equal(run('quoteRanges("只有这里", "没有的引文").length'),0);
 assert.deepEqual(Array.from(run(`quoteRanges('他说：“你好。”','他说:"你好。"')`)[0]),[0,8]);
 elements.get('body').value='重复。中间。重复。';run('locateQuote("重复。")');assert.equal(elements.get('body').selectionStart,0);run('locateQuote("重复。")');assert.equal(elements.get('body').selectionStart,6);
 await click('project-new');assert(elements.get('new-project-dialog').open);
 elements.get('project-name').value='UI 新项目';await click('create-project');
 assert(!elements.get('new-project-dialog').open);assert.equal(run('state.project_name'),'UI 新项目');assert(!elements.get('config').open);
 elements.get('title').value='新章节';await click('save-card');
 elements.get('body').value='尚未定稿';await click('manual');
 elements.get('override').checked=true;await click('approve');
 assert.equal(run('state.can_comment'),true);assert(elements.get('comment').disabled);
 modelServer=require('http').createServer((req,res)=>{let data='';req.on('data',part=>data+=part);req.on('end',()=>{
   const request=JSON.parse(data), input=JSON.parse(request.messages[1].content);let answer;
   if(request.model==='writer') answer={body:input.previous_draft ? '来自写手的修订正文。' : '来自写手的正文。'};
   else if(request.model==='reviewer'){
     reviewCalls++;
     assert.equal(elements.get('body').value,input.draft,'body must be displayed before any model review request');
     assert.equal(elements.get('body').disabled,false,'body remains readable and selectable');
     assert.equal(elements.get('body').readOnly,true);
     answer={issues:[],logic:'章内逻辑清楚',ai_style:'未发现明显模板化表达',pacing:'场景推进自然',extra:'harmless'};
     if(reviewMode==='fail'||(reviewMode==='retry'&&reviewCalls===1))answer=null;
   } else {
     modelCalls++;
     assert.equal(input.final_chapters[0].body,'来自写手的修订正文。');
     assert.equal(input.commentator_persona,'一位直率的老编辑');
     if(readerFail){res.statusCode=503;res.end('{}');return;}
     if(input.discussion){assert.equal(input.discussion.at(-1).role,'user');answer={body:'我说得具体一些：可以让这个动作承担信息。'};}
     else {assert.deepEqual(Object.keys(input),['final_chapters','commentator_persona']);answer={body:'读完后，我觉得动作可以再具体一点。'};}
   }
   res.setHeader('Content-Type','application/json');res.end(JSON.stringify({choices:[{finish_reason:'stop',message:{content:answer===null?'请提供正文':JSON.stringify(answer)}}]}));
 });});
 await new Promise(resolve=>modelServer.listen(0,'127.0.0.1',resolve));
 const base='http://127.0.0.1:'+modelServer.address().port+'/v1';
 async function configure(role,model){elements.get(role+'-provider').value='custom_endpoint';elements.get(role+'-protocol').value='chat';elements.get(role+'-base').value=base+'/arbitrary?route=chat';elements.get(role+'-model').value=model;await click(role+'-save');}
 await configure('writer','writer');
 elements.get('auto-review').checked=false;elements.get('auto-review').onchange();await wait();
 await click('generate');assert.equal(elements.get('body').value,'来自写手的正文。');assert.equal(reviewCalls,0);assert.equal(run('draft.report'),null);
 await configure('reviewer','reviewer');await click('review');assert.equal(reviewCalls,2);assert(run('draft.report'));
 elements.get('auto-review').checked=true;elements.get('auto-review').onchange();await wait();
 reviewMode='fail';elements.get('instruction').value='改一下';
 await elements.get('revise').onclick();await wait();
 assert.equal(elements.get('status').dataset.tone,'error');assert(elements.get('status').textContent.includes('正文已保存并显示'));
 assert.equal(elements.get('body').value,'来自写手的修订正文。');assert.equal(run('draft.report'),null);assert.equal(reviewCalls,4);
 reviewMode='good';await click('review');assert.equal(reviewCalls,5);await click('approve');
 await configure('commentator','reader');assert(!elements.get('comment').disabled);assert.equal(modelCalls,0);
 elements.get('commentator-persona').value='一位直率的老编辑';
 await click('comment');assert(elements.get('commentary-body').textContent.includes('读完后'));assert.equal(modelCalls,1);assert.equal(elements.get('commentary-reply-area').hidden,false);
 elements.get('comment-reply').value='请解释你的建议';await click('send-comment-reply');assert.equal(run('state.commentary.messages.length'),2);assert.equal(elements.get('commentary-discussion').children.length,2);assert.equal(elements.get('comment-reply').value,'');
 readerFail=true;elements.get('comment-reply').value='失败时不要丢掉这条输入';await elements.get('send-comment-reply').onclick();await wait();assert.equal(elements.get('status').dataset.tone,'error');assert.equal(elements.get('comment-reply').value,'失败时不要丢掉这条输入');assert.equal(run('state.commentary.messages.length'),2);readerFail=false;
 const deleteCount=()=>routes.filter(r=>r.route.startsWith('action/delete-comment?')).length;
 const beforeDelete=deleteCount();allowConfirm=false;elements.get('delete-comment').onclick();assert.equal(deleteCount(),beforeDelete);assert(run('state.commentary'));assert(confirmations.at(-1).includes('确定删除'));
 allowConfirm=true;await click('delete-comment');assert.equal(deleteCount(),beforeDelete+1);assert.equal(run('state.commentary'),null);assert.equal(elements.get('commentary-reply-area').hidden,true);assert.equal(elements.get('comment-reply').value,'');assert.equal(elements.get('commentary-discussion').children.length,0);assert.equal(elements.get('commentator-persona').value,'一位直率的老编辑');assert.equal(elements.get('body').value,'来自写手的修订正文。');
 await run('load(active,draft.id)');run('updateButtons()');assert.equal(elements.get('commentary-reply-area').hidden,true);
 await click('comment');
 const callsBefore=modelCalls;
 elements.get('body').value='新版本';await click('manual');assert(elements.get('comment').disabled);assert(elements.get('send-comment-reply').disabled);assert(elements.get('commentary-hint').textContent.includes('过期'));assert.equal(modelCalls,callsBefore);
 console.log('PASS: actual UI event script + live HTTP server: initial collapse, field order, new-project dialog, pacing and requirements, generation/revision/review/finalization, directory relocation, direct project save, optional commentator configuration, manual commentary, final-only payload, gating and stale display; body-before-review ordering, auto toggle and manual review, retry/failure preservation, quote selection, persona, reply success/failure and confirmed/cancelled deletion.');
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(()=>{for(const id of intervals)clearInterval(id);if(modelServer)modelServer.close();proc.kill();fs.rmSync(root,{recursive:true,force:true});});
