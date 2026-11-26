import {laserState} from './lasers.mjs';

export const HOP_COOLDOWN_MS=280;
export const HOP_ANIMATION_MS=140;

const CIVILIAN_CARS=['#e69a35','#bd62d8','#42b684','#e26861'].map(body=>({body,stripe:null,marking:null,beacon:false}));
const RKN_CARS=[{body:'#f3f6fa',stripe:'#2459a3'},{body:'#b5cae2',stripe:'#18385d'},{body:'#dce3eb',stripe:'#3475b8'}].map(car=>({...car,marking:'РКН',beacon:true}));
export function vehicleStyle(difficulty,index){
 const cars=difficulty==='hard'?RKN_CARS:CIVILIAN_CARS;
 return cars[index%cars.length];
}

export function clockElapsed(clock,now){
 const passed=Math.max(0,(now-clock.local)/1000);
 const correction=Math.sign(clock.correction)*Math.min(Math.abs(clock.correction),passed*.1);
 return clock.elapsed+passed+correction;
}

export function syncClock(clock,elapsed,now){
 return {elapsed:clockElapsed(clock,now),local:now,correction:elapsed-clockElapsed(clock,now)};
}

export function visualRow(animation,now,fallback){
 if(!animation)return fallback;
 const progress=Math.max(0,Math.min(1,(now-animation.started)/animation.duration));
 const eased=1-(1-progress)**3;
 return animation.from+(animation.target-animation.from)*eased;
}

export function hopUnapplied(previous,next,direction){
 return direction==='up'&&next.status==='playing'&&next.token===previous.token&&next.seq>previous.seq&&next.y===previous.y;
}

export function reconcileHop(animation,now,state){
 if(!animation)return null;
 if(state.y===animation.target)return {...animation,confirmed:true};
 const position=visualRow(animation,now,state.y);
 if(state.status!=='playing'){
  // A collision ends this attempted hop in place. There is no backward move.
  return {from:position,target:position,started:now,duration:1,confirmed:true,hold:true};
 }
 return {from:position,target:state.y,started:now,duration:90,confirmed:true};
}

export function vehiclePosition(lane,vehicle,elapsed){
 const trackLength=lane.trackLength??9,count=lane.count??1;
 const wrapped=(lane.phase+trackLength*vehicle/count+lane.speed*elapsed)%trackLength;
 return (wrapped<0?wrapped+trackLength:wrapped)-1;
}

// A hint to ask the server for a fresh result, never a local loss or win.
export function collisionHint(game,elapsed){
 for(const lane of game.lanes){
  if(lane.row!==game.y)continue;
  const count=lane.count??1;
  for(let vehicle=0;vehicle<count;vehicle++){
   const car=vehiclePosition(lane,vehicle,elapsed);
   if(Math.abs(car-game.x)<(lane.kind==='mower'?.65:.88))return true;
   if(laserState(lane,vehicle,elapsed)==='fire'){
    const direction=lane.speed>0?1:-1;
    const near=car+direction*.65,far=car+direction*(.65+lane.laser.length);
    if(game.x>Math.min(near,far)-.25&&game.x<Math.max(near,far)+.25)return true;
   }
  }
 }
 return false;
}
