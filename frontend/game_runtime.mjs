import {laserState} from './lasers.mjs';

export const HOP_COOLDOWN_MS=280;
export const HOP_ANIMATION_MS=140;

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

// A hint to ask the server for a fresh result, never a local loss or win.
export function collisionHint(game,elapsed){
 for(const lane of game.lanes){
  if(lane.row!==game.y)continue;
  const count=lane.count??1;
  for(let vehicle=0;vehicle<count;vehicle++){
   const car=((lane.phase+9*vehicle/count+lane.speed*elapsed)%9+9)%9-1;
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
