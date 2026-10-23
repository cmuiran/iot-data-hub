// Alarm annunciator: horn (Web Audio, no sound files) and desktop notifications.
//
// A new unacknowledged alarm of at least `minSeverity` sounds the horn, which repeats until
// every such alarm is acknowledged or the operator presses "Silence" (a newer alarm sounds
// again). Desktop notifications are shown while the tab is in the background.

import { store, bus, severityRank } from './store.js';
import { t } from './i18n.js';

const KEY = 'hub.alarm_prefs';
const REPEAT_MS = 12_000;

function loadPrefs() {
  try { return { sound: true, notify: false, minSeverity: 'high', ...JSON.parse(localStorage.getItem(KEY) || '{}') }; } catch { return { sound: true, notify: false, minSeverity: 'high' }; }
}

class Annunciator extends EventTarget {
  constructor() {
    super();
    this.prefs = loadPrefs();
    this.seen = new Set();
    this.silencedUpTo = 0;    // alarm ids up to this one are silenced
    this.ringing = false;
    this.timer = null;
    this.ctx = null;
    this.started = false;
    this.onAlarms = () => this.check();
  }

  get soundOn() { return this.prefs.sound; }

  save() {
    try { localStorage.setItem(KEY, JSON.stringify(this.prefs)); } catch { /* storage blocked */ }
    this.dispatchEvent(new Event('change'));
  }

  setPrefs(patch) { Object.assign(this.prefs, patch); this.save(); this.check(); }

  toggleSound() {
    this.prefs.sound = !this.prefs.sound;
    if (this.prefs.sound) this.unlock();
    this.save();
    this.check();
  }

  async enableNotifications() {
    if (!('Notification' in window)) return 'unsupported';
    const result = Notification.permission === 'granted' ? 'granted' : await Notification.requestPermission();
    this.prefs.notify = result === 'granted';
    this.save();
    return result;
  }

  /** Browsers only allow audio after a user gesture; call from a click. */
  unlock() {
    try {
      if (!this.ctx) this.ctx = new (window.AudioContext || window.webkitAudioContext)();
      if (this.ctx.state === 'suspended') this.ctx.resume();
    } catch { this.ctx = null; }
  }

  horn() {
    if (!this.prefs.sound || !this.ctx || this.ctx.state !== 'running') return;
    const now = this.ctx.currentTime;
    for (let i = 0; i < 3; i += 1) {
      const osc = this.ctx.createOscillator();
      const gain = this.ctx.createGain();
      osc.type = 'square';
      osc.frequency.value = i % 2 ? 660 : 880;
      gain.gain.setValueAtTime(0.0001, now + i * 0.32);
      gain.gain.exponentialRampToValueAtTime(0.12, now + i * 0.32 + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + i * 0.32 + 0.28);
      osc.connect(gain).connect(this.ctx.destination);
      osc.start(now + i * 0.32);
      osc.stop(now + i * 0.32 + 0.3);
    }
  }

  loud() {
    const min = severityRank(this.prefs.minSeverity);
    return store.alarms.filter((a) => !a.acked_at && !a.shelved && severityRank(a.severity) >= min);
  }

  check() {
    const loud = this.loud();
    const fresh = [];
    for (const a of store.alarms) {
      if (!this.seen.has(a.alarm_id)) { this.seen.add(a.alarm_id); if (!a.acked_at && !a.shelved) fresh.push(a); }
    }
    if (this.prefs.notify && document.visibilityState !== 'visible' && 'Notification' in window && Notification.permission === 'granted') {
      for (const a of fresh.filter((x) => severityRank(x.severity) >= severityRank(this.prefs.minSeverity))) {
        try {
          const n = new Notification(`${t(`severity.${a.severity}`)} · ${t('alarms.notification_title')}`, { body: a.message, tag: `alarm-${a.alarm_id}` });
          n.onclick = () => { window.focus(); location.hash = '#/alarms'; n.close(); };
        } catch { /* notifications may be blocked by policy */ }
      }
    }
    const ring = loud.some((a) => a.alarm_id > this.silencedUpTo);
    if (ring && !this.ringing) {
      this.ringing = true;
      this.horn();
      clearInterval(this.timer);
      this.timer = setInterval(() => this.horn(), REPEAT_MS);
    } else if (!ring && this.ringing) {
      this.ringing = false;
      clearInterval(this.timer);
    }
    this.dispatchEvent(new Event('change'));
  }

  silence() {
    this.silencedUpTo = Math.max(0, ...store.alarms.map((a) => a.alarm_id));
    this.check();
  }

  start() {
    if (this.started) return;
    this.started = true;
    // alarms that are already active when the page opens are not "new" for the notification,
    // but they still sound until silenced or acknowledged
    store.alarms.forEach((a) => this.seen.add(a.alarm_id));
    bus.addEventListener('alarms', this.onAlarms);
    const unlockOnce = () => { this.unlock(); if (this.ringing) this.horn(); };
    document.addEventListener('pointerdown', unlockOnce, { once: true });
    document.addEventListener('keydown', unlockOnce, { once: true });
    this.check();
  }

  stop() {
    this.started = false;
    bus.removeEventListener('alarms', this.onAlarms);
    clearInterval(this.timer);
    this.ringing = false;
    this.seen.clear();
    this.silencedUpTo = 0;
  }
}

export const annunciator = new Annunciator();
