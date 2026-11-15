// Small roof lights, drawn in the existing canvas loop. No timers or network I/O.
export function drawBeacon(ctx,x,y,seconds,reducedMotion=false){
 const redActive=Math.floor(seconds*2)%2===0;
 ctx.save();
 ctx.fillStyle='#172331';ctx.fillRect(x,y,26,7);
 ctx.fillStyle=reducedMotion||redActive?'#ff3658':'#641e31';
 ctx.fillRect(x+1,y+1,11,5);
 ctx.fillStyle=reducedMotion||!redActive?'#349cff':'#193a64';
 ctx.fillRect(x+14,y+1,11,5);
 ctx.restore();
}
