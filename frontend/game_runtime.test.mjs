import test from 'node:test';
import assert from 'node:assert/strict';
import {clockElapsed,syncClock,collisionHint,HOP_COOLDOWN_MS,visualRow,hopUnapplied,reconcileHop,vehicleStyle} from './game_runtime.mjs';

test('RTT clock corrections never move the traffic clock backward',()=>{
 const first={elapsed:5,local:1000,correction:0};
 const corrected=syncClock(first,4.5,1100);
 assert.equal(clockElapsed(corrected,1100),5.1);
 assert(clockElapsed(corrected,1200)>clockElapsed(corrected,1100));
});

test('Confirmed hop keeps its forward animation without changing authoritative game data',()=>{
 const state={y:2};const animation={from:2,target:3,started:100,duration:140};
 assert.equal(HOP_COOLDOWN_MS,280);
 assert.equal(visualRow(animation,100,state.y),2);
 assert(visualRow(animation,170,state.y)>2&&visualRow(animation,170,state.y)<3);
 assert.equal(visualRow(animation,240,state.y),3);
 assert.equal(state.y,2);
 const confirmed=reconcileHop(animation,250,{y:3,status:'playing'});
 assert.equal(confirmed.target,3);assert(confirmed.confirmed);
 assert.equal(visualRow(confirmed,330,3),3);
});

test('An acknowledged but unapplied hop is retried as the same intent, not rolled backward',()=>{
 const previous={token:'one',seq:2,y:2,status:'playing'};
 const early={...previous,seq:3};
 assert(hopUnapplied(previous,early,'up'));
 assert(!hopUnapplied(previous,early,'wait'));
 assert(!hopUnapplied(previous,{...early,y:3},'up'));
 assert(!hopUnapplied(previous,{...early,token:'another'},'up'));
 assert(!hopUnapplied(previous,{...early,seq:2},'up'));
 assert(!hopUnapplied(previous,{...early,status:'lost'},'up'));
});

test('A collision freezes the attempted hop and never animates or snaps backward',()=>{
 const animation={from:2,target:3,started:100,duration:140};
 for(const at of [110,170,240,600]){
  const shown=visualRow(animation,at,2);
  const terminal=reconcileHop(animation,at,{y:2,status:'lost'});
  assert(terminal.hold&&terminal.confirmed);
  for(const later of [at,at+100,at+1000])assert.equal(visualRow(terminal,later,2),shown);
 }
});

test('Easy traffic is civilian and hard traffic retains RKN markings and beacons',()=>{
 const bodies=new Set();
 for(let index=0;index<8;index++){
  const easy=vehicleStyle('easy',index),hard=vehicleStyle('hard',index);
  assert.equal(easy.marking,null);assert.equal(easy.stripe,null);assert.equal(easy.beacon,false);
  assert.equal(hard.marking,'РКН');assert(hard.stripe);assert(hard.beacon);
  bodies.add(easy.body);
  assert.strictEqual(vehicleStyle('easy',index),easy);
 }
 assert.equal(bodies.size,4);
});

test('Collision hint uses cars, mower and laser warnings but never changes the authoritative status',()=>{
 const game={x:3,y:1,status:'playing',lanes:[{row:1,kind:'car',count:1,phase:6,speed:-.001,
  laser:{period:3,warning:.6,duration:.75,length:2,offset:0}}]};
 assert.equal(collisionHint(game,.59),false);
 assert.equal(collisionHint(game,.7),true);
 assert.equal(game.status,'playing');
 game.lanes[0]={row:1,kind:'mower',count:1,phase:4,speed:.6};
 assert.equal(collisionHint(game,0),true);
 game.y=0;assert.equal(collisionHint(game,0),false);
});
