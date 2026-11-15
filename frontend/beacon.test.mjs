import test from 'node:test';
import assert from 'node:assert/strict';
import {drawBeacon} from './beacon.mjs';

function render(seconds,reducedMotion=false){
 const rects=[];let saves=0,restores=0;
 const ctx={fillStyle:'',save(){saves++;},restore(){restores++;},
  fillRect(...box){rects.push({color:this.fillStyle,box});}};
 drawBeacon(ctx,20,30,seconds,reducedMotion);
 assert.equal(saves,1);assert.equal(restores,1);
 assert.equal(rects.length,3);
 assert.deepEqual(rects.map(r=>r.box),[[20,30,26,7],[21,31,11,5],[34,31,11,5]]);
 return rects.map(r=>r.color);
}

test('Roof lights alternate at one cycle per second, with fixed geometry',()=>{
 assert.deepEqual(render(0),['#172331','#ff3658','#193a64']);
 assert.deepEqual(render(.49),render(0));
 assert.deepEqual(render(.5),['#172331','#641e31','#349cff']);
 assert.deepEqual(render(1),render(0));
});

test('Reduced motion keeps both lights static',()=>{
 assert.deepEqual(render(0,true),['#172331','#ff3658','#349cff']);
 assert.deepEqual(render(.5,true),render(0,true));
});
