import test from 'node:test';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
import {runInNewContext} from 'node:vm';
import {buildSync} from 'esbuild';

const appCode=buildSync({entryPoints:[fileURLToPath(new URL('./src.jsx',import.meta.url))],
 bundle:true,write:false,format:'cjs',external:['react','react-dom/client']}).outputFiles[0].text;

function* elements(node){
 if(!node||typeof node!=='object')return;
 yield node;
 for(const child of node.props.children.flat())yield* elements(child);
}
function label(node){
 return node&&typeof node==='object'?node.props.children.flat().map(label).join(''):node??'';
}
const party=(difficulty,status,extra)=>({difficulty,status,seq:0,started:1,now:1,rows:9,lanes:[],...extra});

function fixture({mode='enabled',live=false,pathname='/game/',ranking=false}={}){
 let app,index=0,timerId=0;
 const hooks=[],sockets=[],requests=[],timers=new Map();
 const react={
  createElement:(type,props,...children)=>({type,props:{...props,children}}),
  useState(initial){
   const id=index++;if(!(id in hooks))hooks[id]=initial;
   return [hooks[id],value=>{hooks[id]=typeof value==='function'?value(hooks[id]):value;}];
  },
  useRef(initial){return hooks[index++]??={current:initial};},
  useEffect(){},
 };
 class Socket{
  static OPEN=1;
  sent=[];readyState=1;bufferedAmount=0;
  constructor(){sockets.push(this);}
  send(data){this.sent.push(JSON.parse(data));}
  receive(event){this.onmessage({data:JSON.stringify(event)});}
  state(action,state){this.receive({type:'state',action,state});}
  close(){this.readyState=3;this.onclose?.();}
 }
 const meta={'frog-mode-selection':mode,'frog-live-transport':live?'websocket':undefined,'game-client-version':'fixture'};
 const context={
  require:name=>name==='react'?react:{createRoot:()=>({render:root=>{app=root;}})},
  window:{Telegram:{WebApp:{initData:'fixture-auth',ready(){},expand(){}}},matchMedia:()=>({matches:false})},
  document:{querySelector:selector=>({content:meta[selector.split('"')[1]]}),getElementById:()=>({})},
  location:new URL(`https://fixture.invalid${pathname}?giveaway=7${ranking?'&ranking=1':''}`),URL,URLSearchParams,performance,AbortController,
  WebSocket:Socket,
  setTimeout(callback,delay){const id=++timerId;timers.set(id,{callback,delay});return id;},
  clearTimeout:id=>timers.delete(id),
  async fetch(url,{body}){
   const data=JSON.parse(body);requests.push({url,data});
   return Response.json(party(data.difficulty??'hard','playing',{hardAttemptsLeft:data.difficulty==='hard'?2:3}));
  },
 };
 runInNewContext(appCode,context);
 const find=predicate=>{index=0;return [...elements(app.type())].find(predicate);};
 return {
  sockets,requests,
  modes:()=>find(element=>element.props.className==='mode mode-buttons')?.props.children,
  start:()=>find(element=>element.props.className==='start'),
  board:()=>find(element=>element.props.className==='board'),
  status:()=>label(find(element=>element.props.className==='status')),
  budget:()=>label(find(element=>element.props['aria-live']==='polite')),
  connect(){const socket=sockets.at(-1);socket.onopen();socket.receive({type:'ready'});return socket;},
  runTimer(delay){
   const [id,{callback}]=[...timers].find(([,timer])=>timer.delay===delay);
   timers.delete(id);callback();
  },
 };
}

test('Selecting a raffle mode is local; explicit starts replace the party but reconnects only resume',async()=>{
 const app=fixture({live:true});
 assert.equal(app.modes()[0].props['aria-pressed'],true);
 assert.equal(app.board().props.style.aspectRatio,'7/7');
 assert.match(app.budget(),/3 из 3/);
 const firstStart=app.start().props.onClick();
 const easy=app.connect();await firstStart;
 assert.deepEqual(easy.sent.at(-1),{action:'start',giveaway:7,difficulty:'easy'});
 easy.state('start',party('easy','playing',{hardAttemptsLeft:3}));

 easy.close();app.runTimer(250);
 const resumed=app.connect();
 assert.deepEqual(resumed.sent,[{action:'hello',initData:'fixture-auth',clientVersion:'fixture'},{action:'resume',giveaway:7}]);
 resumed.state('resume',party('easy','playing',{hardAttemptsLeft:3}));
 app.modes()[1].props.onClick();
 assert.equal(app.sockets.length,2);
 assert.equal(resumed.readyState,1);
 assert.match(app.budget(),/3 из 3/);
 assert.match(label(app.start()),/сложную/);

 const hardStart=app.start().props.onClick();
 assert.equal(resumed.readyState,3);
 const hard=app.connect();await hardStart;
 assert.deepEqual(hard.sent.at(-1),{action:'start',giveaway:7,difficulty:'hard'});
 hard.state('start',party('hard','playing',{hardAttemptsLeft:2}));
 resumed.state('sync',party('easy','won',{registration:'pending',hardAttemptsLeft:3}));
 hard.state('move',party('hard','lost'));
 assert.match(app.budget(),/2 из 3/);
 assert.equal(app.modes()[0].props.disabled,false);
 assert.equal(app.start().props.disabled,false);
});

test('Pending registration blocks switches; confirmed participation survives a hard run and first finish',async()=>{
 const app=fixture({live:true});
 const firstStart=app.start().props.onClick();
 const easy=app.connect();await firstStart;
 easy.state('start',party('easy','won',{registration:'pending',hardAttemptsLeft:2}));
 assert.equal(easy.readyState,1);
 assert.ok(app.modes().every(button=>button.props.disabled));
 assert.equal(app.start().props.disabled,true);
 assert.match(app.status(),/Проверяем подписку/);
 easy.state('sync',party('easy','won',{registration:'registered',hardAttemptsLeft:2}));
 assert.equal(app.modes()[1].props.disabled,false);
 assert.equal(app.start().props.disabled,true);
 app.modes()[1].props.onClick();
 assert.match(app.status(),/участие засчитано/);
 const hardStart=app.start().props.onClick();
 const hard=app.connect();await hardStart;
 hard.state('start',party('hard','playing',{hardAttemptsLeft:1}));
 assert.match(app.status(),/Участие засчитано/);
 hard.state('move',party('hard','won',{registration:'registered',hardAttemptsLeft:0}));
 assert.match(app.status(),/Первый финиш.*\/top/);
 assert.match(app.budget(),/0 из 3/);
 assert.ok(app.modes().every(button=>button.props.disabled));
});

test('A confirmed easy win with zero hard attempts offers no new start',async()=>{
 const app=fixture({live:true});
 const started=app.start().props.onClick();
 const socket=app.connect();await started;
 socket.state('start',party('easy','won',{registration:'registered',hardAttemptsLeft:0}));
 assert.match(app.budget(),/0 из 3/);
 assert.ok(app.modes().every(button=>button.props.disabled));
 assert.equal(app.start().props.disabled,true);
});

test('A registered hard winner can start another hard party while attempts remain',async()=>{
 const app=fixture({live:true});
 app.modes()[1].props.onClick();
 const firstStart=app.start().props.onClick();
 const first=app.connect();await firstStart;
 first.state('start',party('hard','playing',{hardAttemptsLeft:2}));
 first.state('move',party('hard','won',{registration:'registered',hardAttemptsLeft:2}));
 assert.match(app.budget(),/2 из 3/);
 assert.equal(app.modes()[0].props.disabled,true);
 assert.equal(app.start().props.disabled,false);
 assert.match(label(app.start()),/сложную/);

 const nextStart=app.start().props.onClick();
 assert.equal(first.readyState,3);
 const next=app.connect();await nextStart;
 assert.deepEqual(next.sent.at(-1),{action:'start',giveaway:7,difficulty:'hard'});
 next.state('start',party('hard','playing',{hardAttemptsLeft:1}));
 assert.match(app.budget(),/1 из 3/);
 assert.match(app.status(),/Участие засчитано/);
});

test('ranking=1 defaults to hard without marking participation as registered',async()=>{
 const app=fixture({ranking:true});
 assert.equal(app.modes()[1].props['aria-pressed'],true);
 assert.equal(app.modes()[0].props.disabled,false);
 assert.doesNotMatch(app.status(),/участие засчитано/i);

 await app.start().props.onClick();
 assert.equal(app.requests[0].data.difficulty,'hard');
 assert.equal(app.modes()[0].props.disabled,false);
 assert.doesNotMatch(app.status(),/участие засчитано/i);
});

test('HTTP fallback uses the selected mode; flag-off raffle and free game keep their legacy payloads',async()=>{
 const app=fixture({live:true});
 app.modes()[1].props.onClick();
 const started=app.start().props.onClick();
 const socket=app.connect();await started;
 socket.state('start',{transport:'http'});
 await Promise.resolve();
 assert.deepEqual(app.requests[0],{url:'/game/api/start',data:{initData:'fixture-auth',clientVersion:'fixture',giveaway:'7',difficulty:'hard'}});

 const legacy=fixture({mode:'disabled'});
 assert.equal(legacy.modes(),undefined);
 await legacy.start().props.onClick();
 assert.deepEqual(legacy.requests[0].data,{initData:'fixture-auth',clientVersion:'fixture',giveaway:'7'});
 const legacyLive=fixture({mode:'disabled',live:true});
 const legacyStart=legacyLive.start().props.onClick();
 const legacySocket=legacyLive.connect();await legacyStart;
 assert.deepEqual(legacySocket.sent.at(-1),{action:'start',giveaway:7});

 const free=fixture({pathname:'/play/',live:true});
 assert.equal(label(free.modes()[1]),'Сложный');
 assert.equal(free.budget(),'');
 free.modes()[1].props.onClick();
 await free.start().props.onClick();
 assert.equal(free.requests[0].data.difficulty,'hard');
 assert.equal(free.sockets.length,0);
});
