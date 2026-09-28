// plugin.js runtime load/render check (F-16 runtime-loading faults + UI contract).
// Loads the real desktop/plugin.js under stubbed SDK modules, renders the pane
// component once, and drives the keyboard shortcut handler — asserting the same
// POST-only contract the backend enforces.
import { readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

function assert(cond, msg) {
  if (!cond) {
    console.error('FAIL:', msg)
    process.exit(1)
  }
}

const here = dirname(fileURLToPath(import.meta.url))
const src = join(here, '..', '..', 'desktop', 'plugin.js')
const copy = join(here, 'plugin_copy.mjs')
writeFileSync(copy, readFileSync(src, 'utf8'))

globalThis.__effectDisposers = []
globalThis.__notifications = []
const listeners = {}
globalThis.window = {
  addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn) },
  removeEventListener: () => {},
}

const restCalls = []
const ctx = {
  rest: async (path, opts = {}) => {
    restCalls.push({ path, ...(opts || {}) })
    return { ok: true, image: null, lines: [], screenshots: [], apps: [] }
  },
  registerMany(items) { this.registered = items },
  storage: { get: async () => null, set: async () => {}, remove: async () => {} },
}

let mod
try {
  mod = await import(copy)
} catch (e) {
  console.error('FAIL: plugin.js failed to load:', e.message)
  process.exit(1)
}

const plugin = mod.default
assert(plugin && plugin.id === 'android-emulator', 'default export shape')
assert(typeof plugin.register === 'function', 'register() exists')

try {
  plugin.register(ctx)
} catch (e) {
  console.error('FAIL: register() threw:', e.message)
  process.exit(1)
}

assert(Array.isArray(ctx.registered) && ctx.registered.length >= 1, 'pane registered')
const pane = ctx.registered[0]
assert(pane.area === 'panes', "pane area is 'panes'")
assert(typeof pane.render === 'function', 'pane has render()')

let root
try {
  const el = pane.render()
  root = el.type(el.props) // execute the EmulatorPane component body once
} catch (e) {
  console.error('FAIL: pane render threw (runtime load fault):', e.message)
  process.exit(1)
}
assert(root && root.type === 'div', 'component renders a root element')

assert((listeners.keydown || []).length > 0, 'keyboard shortcut handler registered')
const kd = listeners.keydown[0]
const ev = (key) => ({ ctrlKey: true, metaKey: false, key, preventDefault() {}, stopPropagation() {} })
kd(ev('h'))
kd(ev('b'))
kd(ev('s'))
await new Promise((r) => setTimeout(r, 20))

const posts = restCalls.filter((c) => c.method === 'POST')
assert(posts.some((c) => c.path.includes('/input/key/HOME')), 'Ctrl+H must POST input/key/HOME')
assert(posts.some((c) => c.path.includes('/input/key/BACK')), 'Ctrl+B must POST input/key/BACK')
assert(posts.some((c) => c.path === '/screenshot/save'), 'Ctrl+S must POST screenshot/save')
for (const c of restCalls) {
  if (c.path.startsWith('/input/')) {
    assert(c.method === 'POST', `input route called with ${c.method || 'GET'}: ${c.path}`)
  }
}

console.log('PLUGIN_JS_LOAD_CHECK_OK')
