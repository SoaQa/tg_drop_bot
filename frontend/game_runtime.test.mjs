import test from 'node:test';
import assert from 'node:assert/strict';
import {clockElapsed,syncClock,collisionHint,HOP_COOLDOWN_MS,visualRow} from './game_runtime.mjs';

test('RTT clock corrections never move the traffic clock backward',()=>{
 const first={elapsed:5,local:1000,correction:0};
 const corrected=syncClock(first,4.5,1100);
 assert.equal(clockElapsed(corrected,1100),5.1);
 assert(clockElapsed(corrected,1200)>clockElapsed(corrected,1100));
});

test('Provisional hop animates forward and can reconcile backward without changing game data',()=>{
 const state={y:2};const animation={from:2,target:3,started:100,duration:140};
 assert.equal(HOP_COOLDOWN_MS,280);
 assert.equal(visualRow(animation,100,state.y),2);
 assert(visualRow(animation,170,state.y)>2&&visualRow(animation,170,state.y)<3);
 assert.equal(visualRow(animation,240,state.y),3);
 assert.equal(state.y,2);
 assert.equal(visualRow({from:3,target:2,started:240,duration:90},330,state.y),2);
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
