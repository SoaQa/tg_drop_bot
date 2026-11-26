import test from 'node:test';
import assert from 'node:assert/strict';
import {collisionHint,vehiclePosition} from './game_runtime.mjs';

test('Car positions and collision hints honor relaxed track length',()=>{
 const lane={row:1,kind:'car',count:2,phase:8,speed:.675};
 const game={x:3,y:1,lanes:[lane]};
 assert.equal(vehiclePosition(lane,1,0),2.5);
 assert.equal(collisionHint(game,0),true);

 lane.trackLength=9.82;
 assert(Math.abs(vehiclePosition(lane,1,0)-2.09)<1e-9);
 assert.equal(collisionHint(game,0),false);
 assert.equal(collisionHint(game,.5),true);

 lane.phase=.1;
 lane.speed=-.675;
 assert(Math.abs(vehiclePosition(lane,0,1)-8.245)<1e-9);
 assert(Math.abs(vehiclePosition(lane,1,1)-3.335)<1e-9);
 assert.equal(collisionHint(game,1),true);
 game.y=2;
 assert.equal(collisionHint(game,1),false);
});

test('Legacy lanes without a vehicle count still draw one vehicle',()=>{
 const lane={row:1,kind:'car',phase:8,speed:.675};
 assert.equal(vehiclePosition(lane,0,0),7);
 assert.equal(collisionHint({x:3,y:1,lanes:[lane]},0),false);
});
