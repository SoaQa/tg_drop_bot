// Visual-only debris: never modifies the authoritative game state or hit boxes.
export function windParticles(elapsed, height, seed = 0) {
  if (!Number.isFinite(elapsed) || elapsed < 1) return [];
  const cycle = (elapsed - 1) % 9;
  if (cycle > 6) return [];
  const particles = [];
  for (let i = 0; i < 14; i++) {
    const age = cycle - i * 0.11;
    if (age < 0 || age > 4.2) continue;
    const progress = age / 4.2;
    const phase = i * 2.399 + seed * 6.28;
    particles.push({
      x: -45 + progress * 510,
      y: height * (0.16 + ((i * 0.173 + seed * 0.31) % 0.68))
        + Math.sin(age * 3 + phase) * 22,
      angle: phase + age * (i % 2 ? 1.8 : -1.4),
      alpha: Math.min(1, progress * 7, (1 - progress) * 7) * 0.68,
      scale: 1.25 + (i % 4) * 0.15,
      paper: i % 3 === 0,
    });
  }
  return particles;
}

export function drawWind(ctx, elapsed, height, seed, strength = 1) {
  ctx.save();
  const particles=windParticles(elapsed,height,seed);
  for (const particle of particles.slice(0,Math.ceil(particles.length*strength))) {
    ctx.save();
    ctx.translate(particle.x, particle.y);
    ctx.rotate(particle.angle);
    ctx.scale(particle.scale, particle.scale);
    ctx.globalAlpha = particle.alpha*strength;
    if (particle.paper) {
      ctx.fillStyle = '#f1e8ce';
      ctx.beginPath();ctx.moveTo(-12,-8);ctx.quadraticCurveTo(0,-3,12,-9);
      ctx.lineTo(11,9);ctx.quadraticCurveTo(0,4,-11,8);ctx.closePath();ctx.fill();
      ctx.strokeStyle = '#c6b58c';ctx.lineWidth = 1;
      ctx.beginPath();ctx.moveTo(-6,-2);ctx.lineTo(6,-3);ctx.moveTo(-5,3);ctx.lineTo(5,2);ctx.stroke();
    } else {
      ctx.fillStyle = '#d9ac54';
      ctx.beginPath();ctx.moveTo(-16,0);ctx.quadraticCurveTo(0,-13,16,0);
      ctx.quadraticCurveTo(0,12,-16,0);ctx.fill();
      ctx.strokeStyle = '#82693c';ctx.lineWidth = 1;
      ctx.beginPath();ctx.moveTo(-18,0);ctx.lineTo(12,0);ctx.stroke();
    }
    ctx.restore();
  }
  ctx.restore();
}
