import React, {useEffect, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';

const tg=window.Telegram?.WebApp;
tg?.ready(); tg?.expand(); tg?.disableVerticalSwipes?.();
function App(){
 const [game,setGame]=useState(null),[message,setMessage]=useState('Доведите лягушку до верхнего берега.'),[busy,setBusy]=useState(false);
 const current=useRef(null),pending=useRef(false),canvas=useRef(null),sync=useRef(0);
 const gid=new URLSearchParams(location.search).get('giveaway');
 async function call(action,extra){
  const r=await fetch('/test/api/game/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({initData:tg?.initData||'',...extra})});
  if(!r.ok) throw new Error(await r.text());
  return r.json();
 }
 function accept(g){ current.current=g;sync.current=performance.now();setGame(g); }
 async function start(){
  if(pending.current)return; pending.current=true;setBusy(true);
  try{accept(await call('start',{giveaway:gid}));setMessage('Стрелками двигайте лягушку. Между дорогами можно отдохнуть.');}
  catch(e){setMessage(e.message);}finally{pending.current=false;setBusy(false);}
 }
 async function move(direction){
  const g=current.current;if(!g||g.status!=='playing'||pending.current)return;
  pending.current=true;
  try{const n=await call('move',{token:g.token,seq:g.seq,direction});accept(n);
   if(n.status==='won')setMessage('Получилось! Ваше участие засчитано. Можно вернуться в бот.');
   if(n.status==='lost')setMessage('Машина задела лягушку. Ничего страшного, попробуйте ещё!');
   if(n.status==='expired')setMessage('Время игры истекло. Начните ещё раз.');
  }catch(e){setMessage(e.message);current.current=null;setGame(null);}finally{pending.current=false;}
 }
 useEffect(()=>{
  const timer=setInterval(()=>move('wait'),350);
  const handler=e=>{const d={ArrowUp:'up',ArrowDown:'down',ArrowLeft:'left',ArrowRight:'right',w:'up',a:'left',s:'down',d:'right'}[e.key];if(d){e.preventDefault();move(d);}};
  window.addEventListener('keydown',handler);return()=>{clearInterval(timer);window.removeEventListener('keydown',handler);};
 },[]);
 useEffect(()=>{
  let frame;
  const draw=()=>{
   const c=canvas.current,ctx=c.getContext('2d'),g=current.current;
   const W=420,H=480,cw=60,ch=60;
   ctx.clearRect(0,0,W,H);
   for(let y=0;y<8;y++){
    const road=[1,2,3,5,6].includes(y),top=H-(y+1)*ch;
    ctx.fillStyle=road?'#283344':y===7?'#b4f0b2':'#77ba87';ctx.fillRect(0,top,W,ch);
    if(road){ctx.setLineDash([13,15]);ctx.strokeStyle='#8795a6';ctx.beginPath();ctx.moveTo(0,top+ch);ctx.lineTo(W,top+ch);ctx.stroke();ctx.setLineDash([]);}
   }
   ctx.fillStyle='#264e3b';ctx.font='bold 14px system-ui';ctx.textAlign='center';ctx.fillText('ФИНИШ',W/2,26);
   const elapsed=g?g.now-g.started+(g.status==='playing'?(performance.now()-sync.current)/1000:0):0;
   (g?.lanes||[]).forEach((lane,i)=>{
    const x=((lane.phase+lane.speed*elapsed)%9+9)%9-1,px=x*cw+cw/2-39,py=H-(lane.row+1)*ch+12;
    ctx.fillStyle=['#f4b354','#ed7a85','#7acbdd'][i%3];ctx.beginPath();ctx.roundRect(px,py,78,36,9);ctx.fill();
    ctx.fillStyle='#344a60';ctx.fillRect(px+22,py+5,27,26);ctx.fillStyle='#d9f4f7';ctx.fillRect(px+(lane.speed>0?48:14),py+7,10,22);
   });
   const fx=(g?.x??3)*cw+cw/2,fy=H-((g?.y??0)+.5)*ch;
   ctx.fillStyle=g?.status==='lost'?'#ef8595':'#bbe955';ctx.beginPath();ctx.ellipse(fx,fy+4,19,15,0,0,Math.PI*2);ctx.fill();
   for(const dx of [-11,11]){ctx.beginPath();ctx.arc(fx+dx,fy-10,8,0,Math.PI*2);ctx.fill();ctx.fillStyle='#20382d';ctx.beginPath();ctx.arc(fx+dx,fy-12,3,0,Math.PI*2);ctx.fill();ctx.fillStyle='#bbe955';}
   frame=requestAnimationFrame(draw);
  };draw();return()=>cancelAnimationFrame(frame);
 },[]);
 const playing=game?.status==='playing';
 return <main><div className="badge">ТЕСТОВЫЙ РОЗЫГРЫШ · GIGAIDE</div><h1>Лягушка на дороге</h1><p className="intro">Перейдите дорогу. Не спешите, машины медленные.</p><div className="board"><canvas ref={canvas} width="420" height="480" aria-label="Игровое поле: лягушка внизу, финиш наверху"/></div><p className="status" role="status">{message}</p>{!playing?<button className="start" disabled={busy||!tg?.initData} onClick={start}>{busy?'Загрузка...':game?.status==='won'?'Сыграть ещё':'Начать игру'}</button>:<div className="controls"><button onClick={()=>move('up')} aria-label="Вверх">↑</button><div>{[['left','←'],['down','↓'],['right','→']].map(([d,t])=><button key={d} onClick={()=>move(d)} aria-label={d}>{t}</button>)}</div></div>}{!tg?.initData&&<p>Откройте эту игру кнопкой в @bobot1922_bot.</p>}<small>Не влияет на основной розыгрыш. На компьютере работают стрелки и WASD.</small></main>;
}
createRoot(document.getElementById('root')).render(<App/>);
