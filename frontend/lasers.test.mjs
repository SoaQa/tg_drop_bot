import test from 'node:test';
import assert from 'node:assert/strict';
import {laserState,drawLaser} from './lasers.mjs';

const lane={speed:-1.2,laser:{period:3,warning:.6,duration:.75,length:2,offset:0}};
test('Laser warns, fires and cools down with an offset per vehicle',()=>{
 assert.equal(laserState({},0,0),'off');
 assert.equal(laserState(lane,0,.59),'warning');
 assert.equal(laserState(lane,0,.6),'fire');
 assert.equal(laserState(lane,0,1.35),'off');
 assert.equal(laserState(lane,0,3.1),'warning');
 assert.equal(laserState(lane,1,.3),'fire');
});
test('Beam points ahead of the car and restores drawing state',()=>{
 const path=[];let saved=0,restored=0;
 const ctx={save(){saved++;},restore(){restored++;},beginPath(){},stroke(){},
  setLineDash(){},moveTo(...p){path.push(p);},lineTo(...p){path.push(p);}};
 drawLaser(ctx,lane,0,.7,200,100);
 assert.deepEqual(path,[[161,100],[41,100]]);
 assert.equal(saved,1);assert.equal(restored,1);
});
