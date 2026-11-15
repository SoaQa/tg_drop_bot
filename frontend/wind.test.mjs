import test from 'node:test';
import assert from 'node:assert/strict';
import {windParticles, drawWind} from './wind.mjs';

test('Gusts leave quiet intervals and cap debris density and opacity', () => {
  for (const height of [300, 420, 540]) {
    for (let t = 0; t <= 36; t += 0.05) {
      const particles = windParticles(t, height, 0.42);
      assert(particles.length <= 14);
      for (const p of particles) {
        assert(p.alpha >= 0 && p.alpha <= 0.68);
        assert(p.scale >= 1.25 && p.scale <= 1.7);
        assert(Number.isFinite(p.x) && Number.isFinite(p.y));
        assert(p.y > 0 && p.y < height);
      }
    }
  }
  assert.deepEqual(windParticles(0, 480), []);
  assert.deepEqual(windParticles(9, 480), []);
  assert.notDeepEqual(windParticles(3, 480), windParticles(3.2, 480));
  assert.equal(windParticles(3, 480).length, 14);
});

test('Rendering restores canvas state and requires no game state mutations', () => {
  let balance = 0;
  const ctx = new Proxy({}, {
    get: (_, key) => key === 'save' ? () => balance++
      : key === 'restore' ? () => balance-- : () => {},
    set: () => true,
  });
  drawWind(ctx, 3, 480, 0.2);
  assert.equal(balance, 0);
});
