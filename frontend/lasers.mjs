export function laserState(lane,vehicle,elapsed){
 const laser=lane.laser;if(!laser)return 'off';
 const remainder=(elapsed+laser.offset+vehicle*.4)%laser.period;
 const phase=remainder<0?remainder+laser.period:remainder;
 return phase<laser.warning?'warning':phase<laser.warning+laser.duration?'fire':'off';
}

export function drawLaser(ctx,lane,vehicle,elapsed,cx,cy){
 const state=laserState(lane,vehicle,elapsed);if(state==='off')return;
 const direction=lane.speed>0?1:-1;
 ctx.save();ctx.strokeStyle=state==='fire'?'#ff2848':'#ff957f';
 ctx.globalAlpha=state==='fire'?.95:.55;ctx.lineWidth=state==='fire'?5:2;
 if(state==='warning')ctx.setLineDash([5,6]);
 ctx.beginPath();ctx.moveTo(cx+direction*.65*60,cy);
 ctx.lineTo(cx+direction*(.65+lane.laser.length)*60,cy);ctx.stroke();ctx.restore();
}
