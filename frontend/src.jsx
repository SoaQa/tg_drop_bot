import React, {useEffect, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {drawWind} from './wind.mjs';

const tg=window.Telegram?.WebApp;
const freeMode=location.pathname.startsWith('/play/');
tg?.ready(); tg?.expand(); tg?.disableVerticalSwipes?.();
function App(){
 const [game,setGame]=useState(null),[message,setMessage]=useState('Доведите лягушку до верхнего берега.'),[busy,setBusy]=useState(false);
 const current=useRef(null),pending=useRef(false),canvas=useRef(null),sync=useRef(0),queuedTap=useRef(false);
 const gid=new URLSearchParams(location.search).get('giveaway');
 async function call(action,extra){
  const r=await fetch((freeMode?'/play/api/':'/test/api/game/')+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({initData:tg?.initData||'',...extra})});
  if(!r.ok) throw new Error(await r.text());
  return r.json();
 }
 function accept(g){ current.current=g;sync.current=performance.now();setGame(g); }
 async function start(){
  if(pending.current)return; pending.current=true;setBusy(true);
  try{queuedTap.current=false;accept(await call('start',{giveaway:gid}));setMessage('Тапните в любое место, чтобы прыгнуть вперёд.');}
  catch(e){setMessage(e.message);}finally{pending.current=false;setBusy(false);}
 }
 async function move(direction){
  const g=current.current;if(!g||g.status!=='playing')return;
  if(pending.current){if(direction==='up')queuedTap.current=true;return;}
  pending.current=true;
  try{const n=await call('move',{token:g.token,nonce:g.nonce,seq:g.seq,direction});accept(n);
   if(n.status==='won')setMessage(freeMode?'Получилось! Лягушка добралась. Сыграем ещё?':'Получилось! Ваше участие засчитано. Можно вернуться в бот.');
   if(n.status==='lost')setMessage('Лягушка попала под колёса. Попробуйте ещё!');
   if(n.status==='expired')setMessage('Время игры истекло. Начните ещё раз.');
  }catch(e){setMessage(e.message);current.current=null;setGame(null);}finally{pending.current=false;
   if(queuedTap.current){queuedTap.current=false;move('up');}
  }
 }
 useEffect(()=>{
  const timer=setInterval(()=>move('wait'),350);
  const tap=e=>{if(!e.target.closest('button,a'))move('up');};
  const key=e=>{if((e.code==='Space'||e.code==='Enter')&&!e.target.closest('button,a')){e.preventDefault();move('up');}};
  document.addEventListener('pointerdown',tap);document.addEventListener('keydown',key);
  const resize=()=>document.documentElement.style.setProperty('--app-height',`${Math.min(window.innerHeight,tg?.viewportStableHeight||window.innerHeight)}px`);
  resize();window.addEventListener('resize',resize);tg?.onEvent?.('viewportChanged',resize);
  return()=>{clearInterval(timer);document.removeEventListener('pointerdown',tap);document.removeEventListener('keydown',key);window.removeEventListener('resize',resize);tg?.offEvent?.('viewportChanged',resize);};
 },[]);
 useEffect(()=>{
  let frame;
  const motionPreference=window.matchMedia('(prefers-reduced-motion: reduce)');
  const draw=()=>{
   const c=canvas.current,ctx=c.getContext('2d'),g=current.current;
   const rows=g?.rows??8,W=420,H=rows*60,cw=60,ch=60;
   if(c.height!==H)c.height=H;
   ctx.clearRect(0,0,W,H);
   for(let y=0;y<rows;y++){
    const road=g?g.lanes.some(l=>l.row===y&&l.kind!=='mower'):[1,2,3,5,6].includes(y),top=H-(y+1)*ch;
    ctx.fillStyle=road?'#283344':y===rows-1?'#b4f0b2':'#77ba87';ctx.fillRect(0,top,W,ch);
    if(road){ctx.setLineDash([13,15]);ctx.strokeStyle='#8795a6';ctx.beginPath();ctx.moveTo(0,top+ch);ctx.lineTo(W,top+ch);ctx.stroke();ctx.setLineDash([]);}
   }
   ctx.fillStyle='#264e3b';ctx.font='bold 14px system-ui';ctx.textAlign='center';ctx.fillText('ФИНИШ',W/2,26);
   const elapsed=g?g.now-g.started+(g.status==='playing'?(performance.now()-sync.current)/1000:0):0;
   (g?.lanes||[]).forEach((lane,i)=>{
    const x=((lane.phase+lane.speed*elapsed)%9+9)%9-1,px=x*cw+cw/2-39,py=H-(lane.row+1)*ch+12;
    if(lane.kind==='mower'){
     const mx=px+14;
     ctx.fillStyle='#244533';ctx.fillRect(mx+4,py-3,9,9);ctx.fillRect(mx+4,py+30,9,9);ctx.fillRect(mx+38,py-3,9,9);ctx.fillRect(mx+38,py+30,9,9);
     ctx.fillStyle='#e8a446';ctx.beginPath();ctx.roundRect(mx,py,52,36,7);ctx.fill();
     ctx.fillStyle='#2c6650';ctx.fillRect(mx+14,py+7,23,22);ctx.strokeStyle='#263f36';ctx.lineWidth=3;ctx.beginPath();ctx.moveTo(mx+9,py+9);ctx.lineTo(mx-8,py+3);ctx.lineTo(mx-8,py+33);ctx.lineTo(mx+9,py+27);ctx.stroke();ctx.lineWidth=1;
     return;
    }
    ctx.fillStyle=['#f4b354','#ed7a85','#7acbdd'][i%3];ctx.beginPath();ctx.roundRect(px,py,78,36,9);ctx.fill();
    ctx.fillStyle='#344a60';ctx.fillRect(px+22,py+5,27,26);ctx.fillStyle='#d9f4f7';ctx.fillRect(px+(lane.speed>0?48:14),py+7,10,22);
   });
   const fx=(g?.x??3)*cw+cw/2,fy=H-((g?.y??0)+.5)*ch;
   ctx.fillStyle=g?.status==='lost'?'#ef8595':'#bbe955';ctx.beginPath();ctx.ellipse(fx,fy+4,19,15,0,0,Math.PI*2);ctx.fill();
   for(const dx of [-11,11]){ctx.beginPath();ctx.arc(fx+dx,fy-10,8,0,Math.PI*2);ctx.fill();ctx.fillStyle='#20382d';ctx.beginPath();ctx.arc(fx+dx,fy-12,3,0,Math.PI*2);ctx.fill();ctx.fillStyle='#bbe955';}
   if(g&&!motionPreference.matches)drawWind(ctx,elapsed,H,(g.started%997)/997);
   frame=requestAnimationFrame(draw);
  };draw();return()=>cancelAnimationFrame(frame);
 },[]);
 const playing=game?.status==='playing';
 return <main><div className="badge">{freeMode?'ЛЯГУШКА · МИНИ-ИГРА':'ТЕСТОВЫЙ РОЗЫГРЫШ · GIGAIDE'}</div><h1>Лягушка на дороге</h1><p className="intro">Один тап или клик: один прыжок вперёд.</p><div className="board" style={{aspectRatio:`7/${game?.rows??8}`,maxWidth:`calc((var(--app-height,100dvh) - 218px)*${7/(game?.rows??8)})`}}><canvas ref={canvas} width="420" height="480" tabIndex="0" role="button" aria-label="Прыгнуть вперёд"/></div><p className="status" role="status">{message}</p>{!playing?<button className="start" disabled={busy||!tg?.initData} onClick={start}>{busy?'Загрузка...':game?.status==='won'?'Сыграть ещё':'Начать игру'}</button>:<div className="tap-hint">Берегитесь машин и газонокосилки на траве</div>}{!tg?.initData&&<p className="external">{freeMode?'Откройте игру командой /game в боте.':'Откройте игру кнопкой в @bobot1922_bot.'}</p>}<small>На компьютере также работает пробел.</small></main>;
}
createRoot(document.getElementById('root')).render(<App/>);
