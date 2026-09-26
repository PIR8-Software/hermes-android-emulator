/**
 * Android Emulator — live right-side pane in Hermes Desktop
 */

import {
  cn,
  haptic,
  host,
  useQuery,
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useState, useRef, useCallback, useEffect } from 'react'

const ID = 'android-emulator'
const POLL_MS = 3000

function EmulatorPane({ ctx }) {
  const [showLog, setShowLog] = useState(false)
  const [logcat, setLogcat] = useState([])
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [showPicker, setShowPicker] = useState(false)
  const [showApps, setShowApps] = useState(false)
  const [apps, setApps] = useState([])
  const [textInput, setTextInput] = useState('')
  const [showTools, setShowTools] = useState(false)
  const [shellCmd, setShellCmd] = useState('')
  const [shellOut, setShellOut] = useState('')
  const [showMore, setShowMore] = useState(false)
  const [gallery, setGallery] = useState([])
  const [showGallery, setShowGallery] = useState(false)
  const [gpsLat, setGpsLat] = useState('37.7749')
  const [gpsLng, setGpsLng] = useState('-122.4194')
  const [batteryLevel, setBatteryLevel] = useState(100)
  const [deeplinkUrl, setDeeplinkUrl] = useState('')
  const [notifTitle, setNotifTitle] = useState('Test')
  const [notifBody, setNotifBody] = useState('From Hermes')
  const [isRecording, setIsRecording] = useState(false)
  const [testOutput, setTestOutput] = useState('')
  const [logFilter, setLogFilter] = useState('')
  const [apkPath, setApkPath] = useState('')
  const [isCapturing, setIsCapturing] = useState(false)
  const [tab, setTab] = useState('controls')
  const imgRef = useRef(null)
  // SVG Icons
  const Icon = ({ d, size = 18 }) => jsx("svg", { width: size, height: size, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2, strokeLinecap: "round", strokeLinejoin: "round", children: jsx("path", { d }) })
  const icons = {
    statusBar: "M5 2h14v6H5z M7 7h10v15H7z",  // three horizontal lines (notification shade)
    back: "M19 12H5M12 19l-7-7 7-7",        // arrow pointing left
    home: "M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z M9 22V12h6v10", // house
    recent: "M8 2h8v20H8z M1 6h5v12H1z M18 6h5v12H18z", // grid/squares
    power: "M12 2v6 M18.36 6.64a9 9 0 1 1-12.73 0", // power symbol
    appDrawer: "M5 16h14v6H5z M7 2h10v15H7z", // 2x3 grid
    screenshot: "M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z M12 17a4 4 0 1 0 0-8 4 4 0 0 0 0 8z", // camera
    network: "M6 3h12v10H6z M12 13v8 M4 21h16", // wifi signal
    record: "M12 12m-8 0a8 8 0 1 0 16 0 8 8 0 1 0-16 0", // circle (record)
    shell: "M4 17l6-5-6-5 M12 19h8", // terminal prompt >_
    pause: "M6 4h4v16H6z M14 4h4v16h-4z",  // two vertical bars (pause)
    play: "M5 3l14 9-14 9z",  // triangle (play/live)
    logcat: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z M14 2v6h6 M16 13H8 M16 17H8 M10 9H8",  // document with lines
    stop: "M6 6h12v12H6z",  // square (stop)
  }

  const statusQ = useQuery({
    queryKey: ['emu', 'status'],
    queryFn: () => ctx.rest('/status', { timeoutMs: 8000 }),
    refetchInterval: autoRefresh ? POLL_MS : false,
    retry: 0,
    staleTime: 2000,
  })

  const screenQ = useQuery({
    queryKey: ['emu', 'screen'],
    queryFn: () => ctx.rest('/screenshot_b64', { timeoutMs: 10000 }),
    refetchInterval: autoRefresh ? POLL_MS : false,
    retry: 0,
    staleTime: 2000,
  })

  const avdsQ = useQuery({
    queryKey: ['emu', 'picker'],
    queryFn: () => ctx.rest('/picker', { timeoutMs: 30000 }),
    staleTime: 60000,
    retry: 1,
  })

  const status = statusQ.data || {}
  const screen = screenQ.data || {}
  const picker = avdsQ.data || {}
  const isOnline = !!status?.online
  const imgSrc = screen?.image || null

  const sendKey = useCallback(async (key) => {
    haptic('tap')
    try { await ctx.rest(`/input/key/${key}`, { method: 'POST', timeoutMs: 3000 }) } catch {}
  }, [ctx])

  const sendTap = useCallback(async (x, y) => {
    try { await ctx.rest(`/input/tap/${x}/${y}`, { method: 'POST', timeoutMs: 3000 }) } catch {}
  }, [ctx])

  const handleImageClick = (e) => {
    if (!imgRef.current) return
    const rect = imgRef.current.getBoundingClientRect()
    const sw = parseInt(status?.screen_size?.split('x')[0]) || 1080
    const sh = parseInt(status?.screen_size?.split('x')[1]) || 2400
    sendTap(
      Math.round((e.clientX - rect.left) * sw / rect.width),
      Math.round((e.clientY - rect.top) * sh / rect.height)
    )
  }

  const fetchLogcat = async () => {
    try {
      const q = logFilter ? `&filter=${encodeURIComponent(logFilter)}` : ''
      const d = await ctx.rest(`/logcat?lines=80${q}`, { timeoutMs: 5000 })
      setLogcat(d?.lines || [])
    } catch {}
  }

  const sendSwipe = useCallback(async (dir) => {
    haptic('tap')
    try { await ctx.rest(`/swipe/${dir}`, { method: 'POST', timeoutMs: 3000 }) } catch {}
  }, [ctx])

  const fetchApps = useCallback(async () => {
    try {
      const d = await ctx.rest('/apps', { timeoutMs: 10000 })
      setApps(d?.apps || [])
    } catch {}
  }, [ctx])

  const launchApp = useCallback(async (pkg) => {
    haptic('tap')
    try {
      await ctx.rest(`/apps/launch?package=${encodeURIComponent(pkg)}`, { method: 'POST', timeoutMs: 5000 })
    } catch {}
  }, [ctx])

  const sendText = useCallback(async () => {
    if (!textInput) return
    haptic('tap')
    try {
      await ctx.rest(`/type?text=${encodeURIComponent(textInput)}`, { method: 'POST', timeoutMs: 3000 })
      setTextInput('')
    } catch {}
  }, [ctx, textInput])

  const saveScreenshot = useCallback(async () => {
    haptic('tap')
    try {
      const r = await ctx.rest('/screenshot/save', { method: 'POST', timeoutMs: 5000 })
      if (r?.ok) host.notify({ kind: 'success', message: 'Screenshot saved' })
    } catch {}
  }, [ctx])

  const toggleNetwork = useCallback(async (cond) => {
    haptic('tap')
    try {
      await ctx.rest(`/network/${cond}`, { method: 'POST', timeoutMs: 5000 })
      host.notify({ kind: 'info', message: `Network: ${cond}` })
    } catch {}
  }, [ctx])

  const runShell = useCallback(async () => {
    if (!shellCmd) return
    try {
      const r = await ctx.rest(`/shell?command=${encodeURIComponent(shellCmd)}`, { method: 'POST', timeoutMs: 15000 })
      setShellOut(r?.stdout || 'no output')
    } catch { setShellOut('request failed') }
  }, [ctx, shellCmd])

  const fetchGallery = useCallback(async () => {
    try {
      const d = await ctx.rest('/screenshot/gallery', { timeoutMs: 5000 })
      setGallery(d?.screenshots || [])
    } catch {}
  }, [ctx])

  const setGps = useCallback(async () => {
    haptic('tap')
    try {
      await ctx.rest(`/gps/${gpsLat}/${gpsLng}`, { method: 'POST', timeoutMs: 3000 })
      host.notify({ kind: 'success', message: `GPS: ${gpsLat}, ${gpsLng}` })
    } catch {}
  }, [ctx, gpsLat, gpsLng])

  const setBattery = useCallback(async (lvl) => {
    haptic('tap')
    setBatteryLevel(lvl)
    try { await ctx.rest(`/battery/${lvl}`, { method: 'POST', timeoutMs: 3000 }) } catch {}
  }, [ctx])

  const sendDeeplink = useCallback(async () => {
    if (!deeplinkUrl) return
    haptic('tap')
    try {
      await ctx.rest(`/deeplink?url=${encodeURIComponent(deeplinkUrl)}`, { method: 'POST', timeoutMs: 3000 })
    } catch {}
  }, [ctx, deeplinkUrl])

  const sendNotification = useCallback(async () => {
    haptic('tap')
    try {
      const r = await ctx.rest(`/notification?title=${encodeURIComponent(notifTitle)}&body=${encodeURIComponent(notifBody)}`, { method: 'POST', timeoutMs: 5000 })
      host.notify(r?.ok
        ? { kind: 'success', message: 'Notification sent' }
        : { kind: 'error', message: r?.error || 'Notification not delivered' })
    } catch { host.notify({ kind: 'error', message: 'Notification request failed' }) }
  }, [ctx, notifTitle, notifBody])

  const toggleRecording = useCallback(async () => {
    haptic('tap')
    if (!isRecording) {
      try {
        const r = await ctx.rest('/record/start', { method: 'POST' })
        if (r?.ok) { setIsRecording(true); host.notify({ kind: 'success', message: r.message || 'Recording started' }) }
        else host.notify({ kind: 'error', message: r?.error || 'Recording failed to start' })
      } catch { host.notify({ kind: 'error', message: 'Recording request failed' }) }
    } else {
      try {
        const r = await ctx.rest('/record/stop', { method: 'POST', timeoutMs: 30000 })
        setIsRecording(false)
        host.notify(r?.ok
          ? { kind: 'success', message: `Recording saved: ${(r.path || '').split('/').pop()} (${r.size || 0} bytes)` }
          : { kind: 'error', message: r?.error || 'Recording not saved' })
      } catch { setIsRecording(false); host.notify({ kind: 'error', message: 'Recording stop failed' }) }
    }
  }, [ctx, isRecording])

  const toggleCapture = useCallback(async () => {
    haptic('tap')
    if (!isCapturing) {
      try {
        const r = await ctx.rest('/replay/record/start', { method: 'POST' })
        if (r?.ok) { setIsCapturing(true); host.notify({ kind: 'success', message: r.message || 'Capturing touches' }) }
        else host.notify({ kind: 'error', message: r?.error || 'Capture failed to start' })
      } catch { host.notify({ kind: 'error', message: 'Capture request failed' }) }
    } else {
      try {
        const r = await ctx.rest('/replay/record/stop', { method: 'POST', timeoutMs: 20000 })
        setIsCapturing(false)
        host.notify(r?.ok
          ? { kind: 'success', message: `Saved ${r.gestures || 0} gesture(s)` }
          : { kind: 'error', message: r?.error || 'Capture not saved' })
      } catch { setIsCapturing(false); host.notify({ kind: 'error', message: 'Capture stop failed' }) }
    }
  }, [ctx, isCapturing])

  const playReplay = useCallback(async () => {
    haptic('tap')
    try {
      const r = await ctx.rest('/replay/play', { method: 'POST', timeoutMs: 60000 })
      host.notify(r?.ok
        ? { kind: 'success', message: `Replayed ${r.played || 0} gesture(s)` }
        : { kind: 'error', message: r?.error || 'Replay failed' })
    } catch { host.notify({ kind: 'error', message: 'Replay request failed' }) }
  }, [ctx])

  const installApk = useCallback(async () => {
    if (!apkPath) return
    haptic('tap')
    try {
      const r = await ctx.rest(`/apps/install?apk_path=${encodeURIComponent(apkPath)}`, { method: 'POST', timeoutMs: 120000 })
      host.notify(r?.ok
        ? { kind: 'success', message: 'APK installed' }
        : { kind: 'error', message: r?.error || (r?.output || 'Install failed') })
    } catch { host.notify({ kind: 'error', message: 'Install request failed' }) }
  }, [ctx, apkPath])

  const uninstallApp = useCallback(async (pkg) => {
    haptic('tap')
    try {
      const r = await ctx.rest(`/apps/uninstall?package=${encodeURIComponent(pkg)}`, { method: 'POST', timeoutMs: 30000 })
      host.notify(r?.ok
        ? { kind: 'success', message: `Uninstalled ${pkg}` }
        : { kind: 'error', message: r?.error || `Uninstall failed: ${r?.output || ''}` })
      if (r?.ok) fetchApps()
    } catch { host.notify({ kind: 'error', message: 'Uninstall request failed' }) }
  }, [ctx])

  const switchAvd = useCallback(async (name) => {
    haptic('tap')
    host.notify({ kind: 'info', message: `Switching to ${name}...` })
    try {
      const r = await ctx.rest(`/picker/switch?name=${encodeURIComponent(name)}`, { method: 'POST', timeoutMs: 30000 })
      host.notify(r?.ok
        ? { kind: 'success', message: `Switched to ${name}` }
        : { kind: 'error', message: r?.error || 'Switch failed' })
    } catch { host.notify({ kind: 'error', message: 'Switch request failed' }) }
  }, [ctx])

  const clearGps = useCallback(async () => {
    haptic('tap')
    try {
      const r = await ctx.rest('/gps/clear', { method: 'POST', timeoutMs: 3000 })
      host.notify({ kind: r?.ok ? 'success' : 'error', message: r?.ok ? 'GPS override cleared' : (r?.error || 'GPS clear failed') })
    } catch {}
  }, [ctx])

  const unplugBattery = useCallback(async () => {
    haptic('tap')
    try {
      const r = await ctx.rest('/battery/unplug', { method: 'POST', timeoutMs: 3000 })
      host.notify({ kind: r?.ok ? 'success' : 'error', message: r?.ok ? 'Battery: unplugged (draining)' : (r?.error || 'Battery unplug failed') })
    } catch {}
  }, [ctx])

  const runTests = useCallback(async () => {
    haptic('tap')
    setTestOutput('Running tests...')
    try {
      const r = await ctx.rest('/test/run', { method: 'POST', timeoutMs: 300000 })
      setTestOutput(r?.output || (r?.error || 'No output'))
    } catch { setTestOutput('Test request failed') }
  }, [ctx])

  // ── Keyboard shortcuts (matches GET /shortcuts) ──────────────────────
  useEffect(() => {
    const onKeyDown = (e) => {
      if (!e.ctrlKey && !e.metaKey) return
      const k = e.key.toLowerCase()
      if (k === 's') { e.preventDefault(); e.stopPropagation(); saveScreenshot() }
      else if (k === 'h') { e.preventDefault(); e.stopPropagation(); sendKey('HOME') }
      else if (k === 'b') { e.preventDefault(); e.stopPropagation(); sendKey('BACK') }
      else if (k === 'l') { e.preventDefault(); e.stopPropagation(); setShowLog((v) => { if (!v) fetchLogcat(); return !v }) }
      else if (k === 'g') { e.preventDefault(); e.stopPropagation(); setShowMore(true); setShowGallery((v) => { if (!v) fetchGallery(); return !v }) }
      else if (k === 'r') { e.preventDefault(); e.stopPropagation(); toggleRecording() }
    }
    window.addEventListener('keydown', onKeyDown, true)
    return () => window.removeEventListener('keydown', onKeyDown, true)
  }, [saveScreenshot, sendKey, toggleRecording])

  let screenContent
  if (!isOnline) {
    screenContent = jsx('div', {
      className: 'flex-1 flex flex-col items-center justify-center gap-3 text-zinc-500',
      children: [
        jsx('div', { key: 'i', className: 'text-3xl', children: '📱' }),
        jsx('div', { key: 'm', className: 'text-sm', children: 'Emulator offline' }),
        jsx('button', {
          key: 'start',
          className: 'rounded-lg border border-green-700 bg-green-900/50 px-4 py-2 text-sm text-green-300 hover:bg-green-800/50 active:bg-green-700/50 font-medium',
          onClick: async () => {
            haptic('tap')
            try {
              await ctx.rest('/start', { method: 'POST', timeoutMs: 10000 })
              host.notify({ kind: 'success', message: 'Starting emulator...' })
            } catch { host.notify({ kind: 'error', message: 'Failed to start' }) }
          },
          children: '▶ Start Emulator',
        }),
        jsx('div', { key: 'c', className: 'text-[10px] text-zinc-600', children: 'Takes ~15s to boot' }),
      ],
    })
  } else if (!imgSrc) {
    screenContent = jsx('div', {
      className: 'flex-1 flex items-center justify-center text-zinc-500 text-sm',
      children: 'Loading screen...',
    })
  } else {
    screenContent = jsx('div', {
      className: 'relative mx-auto w-full max-w-[200px] rounded-xl overflow-hidden border-2 border-zinc-700 shadow-lg cursor-pointer bg-black',
      children: jsx('img', {
        ref: imgRef,
        src: imgSrc,
        onClick: handleImageClick,
        alt: 'Emulator',
        className: 'w-full block',
        draggable: false,
      }),
    })
  }

  const Tab = ({ id, label }) => jsx('button', {
    className: cn(
      'flex-1 py-1 text-xs font-medium border-b-2 transition-colors',
      tab === id ? 'border-blue-500 text-blue-300' : 'border-transparent text-zinc-500 hover:text-zinc-300'
    ),
    onClick: () => setTab(id),
    children: label,
  })

  return jsxs('div', {
    className: 'flex h-full flex-col text-xs overflow-hidden',
    children: [

      // ── EMULATOR SCREEN (always visible) ──────────────────────────
      screenContent,

      // ── TAB BAR ──────────────────────────────────────────────────
      jsx('div', {
        className: 'flex border-b border-zinc-700 px-2',
        children: [
          jsx(Tab, { id: 'controls', label: '🎮 Controls' }),
          jsx(Tab, { id: 'device', label: '📱 Device' }),
        ],
      }),

      // ── TAB CONTENT ──────────────────────────────────────────────
      jsxs('div', {
        className: 'flex-1 overflow-y-auto p-2 space-y-1.5',
        children: [

          // ═══════════════════════════════════════════════════════════
          // CONTROLS TAB
          // ═══════════════════════════════════════════════════════════
          tab === 'controls' && jsxs('div', {
            className: 'space-y-1.5',
            children: [
              // Type Text
              jsx('div', {
                className: 'flex gap-1',
                children: [
                  jsx('input', {
                    type: 'text',
                    value: textInput,
                    onChange: (e) => setTextInput(e.target.value),
                    onKeyDown: (e) => { if (e.key === 'Enter') sendText() },
                    placeholder: 'Type text here...',
                    className: 'flex-1 rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-zinc-200 placeholder-zinc-500 outline-none focus:border-blue-600',
                  }),
                  jsx('button', {
                    className: 'rounded border border-blue-700 bg-blue-900/50 px-3 py-1 text-blue-300 hover:bg-blue-800/50',
                    onClick: sendText,
                    children: '⌨️ Send',
                  }),
                ],
              }),

              // Navigation
              jsx('div', {
                className: 'grid grid-cols-6 gap-1',
                children: [
                  ['statusBar', 'Status Bar', async () => { try { const r = await ctx.rest('/statusbar', { method: 'POST', timeoutMs: 5000 }); host.notify({ kind: r?.ok ? 'success' : 'error', message: r?.ok ? 'Status bar opened' : 'Status bar failed' }) } catch (e) { host.notify({ kind: 'error', message: 'Status bar failed: ' + e.message }) } }],
                  ['back', 'Back', 'BACK'],
                  ['home', 'Home', 'HOME'],
                  ['recent', 'Recent', 'APP_SWITCH'],
                  ['power', 'Power', 'POWER'],
                  ['appDrawer', 'App Drawer', async () => { try { const r = await ctx.rest('/appdrawer', { method: 'POST', timeoutMs: 5000 }); host.notify({ kind: r?.ok ? 'success' : 'error', message: r?.ok ? 'App drawer opened' : 'App drawer failed' }) } catch (e) { host.notify({ kind: 'error', message: 'App drawer failed: ' + e.message }) } }],
                ].map(([iconKey, label, action]) =>
                  jsx('button', {
                    key: label,
                    className: 'flex items-center justify-center rounded border border-zinc-700 bg-zinc-800 py-1 text-zinc-200 hover:bg-zinc-700 active:bg-zinc-600',
                    onClick: () => { haptic('tap'); typeof action === 'function' ? action() : sendKey(action) },
                    title: label,
                    children: jsx(Icon, { d: icons[iconKey] }),
                  })
                ),
              }),

              // Swipe
              jsx('div', {
                className: 'grid grid-cols-4 gap-1',
                children: [
                  ['left', 'Swipe left'],
                  ['up', 'Swipe up'],
                  ['down', 'Swipe down'],
                  ['right', 'Swipe right'],
                ].map(([dir, tip]) =>
                  jsx('button', {
                    key: dir,
                    className: 'flex items-center justify-center rounded border border-zinc-700 bg-zinc-800 py-1.5 text-zinc-300 hover:bg-zinc-700 active:bg-zinc-600',
                    onClick: () => sendSwipe(dir),
                    title: tip,
                    children: jsx('svg', {
                      width: '20',
                      height: '20',
                      viewBox: '0 0 20 20',
                      fill: 'none',
                      stroke: 'currentColor',
                      strokeWidth: '2',
                      strokeLinecap: 'round',
                      strokeLinejoin: 'round',
                      children: dir === 'left'
                        ? jsx('path', { d: 'M13 4L7 10L13 16' })
                        : dir === 'right'
                        ? jsx('path', { d: 'M7 4L13 10L7 16' })
                        : dir === 'up'
                        ? jsx('path', { d: 'M4 13L10 7L16 13' })
                        : jsx('path', { d: 'M4 7L10 13L16 7' }),
                    }),
                  })
                ),
              }),


              // Quick Tools
              jsx('div', {
                className: 'grid grid-cols-4 gap-1',
                children: [
                  ['screenshot', 'Screenshot', () => saveScreenshot()],
                  ['network', 'Network', () => setShowTools(!showTools)],
                  ['record', 'Record', () => toggleRecording()],
                  ['shell', 'Shell', () => setShowTools(!showTools)],
                ].map(([iconKey, label, fn]) =>
                  jsx('button', {
                    key: label,
                    className: 'flex items-center justify-center rounded border border-zinc-700 bg-zinc-800 py-1 text-zinc-300 hover:bg-zinc-700',
                    onClick: fn,
                    title: label,
                    children: jsx(Icon, { d: icons[iconKey] }),
                  })
                ),
              }),


              // Network Simulator
              showTools && jsx('div', {
                className: 'rounded border border-zinc-700 bg-zinc-900 p-2 space-y-1.5',
                children: [
                  jsx('div', { className: 'text-zinc-400 font-medium', children: '🌐 Network Condition' }),
                  jsx('div', {
                    className: 'flex gap-1',
                    children: ['offline', 'slow', 'fast'].map((cond) =>
                      jsx('button', {
                        key: cond,
                        className: 'flex-1 rounded border border-zinc-700 bg-zinc-800 py-1 text-zinc-300 hover:bg-zinc-700',
                        onClick: () => toggleNetwork(cond),
                        children: cond,
                      })
                    ),
                  }),
                  jsx('div', { className: 'text-[10px] text-zinc-500', children: 'offline = no data · slow = 500ms latency · fast = normal' }),
                  jsx('div', { className: 'text-zinc-400 font-medium pt-1 border-t border-zinc-800', children: '💻 ADB Shell' }),
                  jsx('div', {
                    className: 'flex gap-1',
                    children: [
                      jsx('input', {
                        type: 'text',
                        value: shellCmd,
                        onChange: (e) => setShellCmd(e.target.value),
                        onKeyDown: (e) => { if (e.key === 'Enter') runShell() },
                        placeholder: 'e.g. ls /sdcard',
                        className: 'flex-1 rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-zinc-200 placeholder-zinc-500 outline-none focus:border-blue-600 font-mono',
                      }),
                      jsx('button', {
                        className: 'rounded border border-blue-700 bg-blue-900/50 px-3 py-1 text-blue-300',
                        onClick: runShell,
                        children: 'Run',
                      }),
                    ],
                  }),
                  shellOut && jsx('pre', {
                    className: 'rounded bg-zinc-950 border border-zinc-800 p-1 text-[10px] text-zinc-400 max-h-[80px] overflow-auto font-mono whitespace-pre-wrap',
                    children: shellOut,
                  }),
                ],
              }),

              // Controls bar
              jsx('div', {
                className: 'flex gap-1',
                children: [
                  jsx('button', {
                    className: cn(
                      'flex-1 flex items-center justify-center gap-1.5 rounded border py-1.5',
                      autoRefresh ? 'border-blue-700 bg-blue-900/50 text-blue-300' : 'border-zinc-700 bg-zinc-800 text-zinc-200'
                    ),
                    onClick: () => setAutoRefresh(!autoRefresh),
                    title: autoRefresh ? 'Pause live view' : 'Resume live view',
                    children: [
                      jsx(Icon, { key: 'i', d: autoRefresh ? icons.pause : icons.play, size: 14 }),
                      jsx('span', { key: 't', children: autoRefresh ? 'Pause' : 'Live' }),
                    ],
                  }),
                  jsx('button', {
                    className: cn(
                      'flex-1 flex items-center justify-center gap-1.5 rounded border py-1.5',
                      showLog ? 'border-amber-700 bg-amber-900/50 text-amber-300' : 'border-zinc-700 bg-zinc-800 text-zinc-200'
                    ),
                    onClick: () => { fetchLogcat(); setShowLog(!showLog) },
                    title: showLog ? 'Hide log output' : 'Show log output',
                    children: [
                      jsx(Icon, { key: 'i', d: icons.logcat, size: 14 }),
                      jsx('span', { key: 't', children: showLog ? 'Hide Log' : 'Logcat' }),
                    ],
                  }),
                  jsx('button', {
                    className: 'flex items-center justify-center gap-1.5 rounded border border-red-700 bg-red-900/50 py-1.5 px-3 text-red-300',
                    onClick: async () => { haptic('tap'); try { await ctx.rest('/stop', { method: 'POST', timeoutMs: 5000 }) } catch {} },
                    title: 'Stop emulator',
                    children: [
                      jsx(Icon, { key: 'i', d: icons.stop, size: 14 }),
                      jsx('span', { key: 't', children: 'Stop' }),
                    ],
                  }),
                ],
              }),

              // Logcat filter
              showLog && jsx('div', {
                key: 'logfilter',
                className: 'flex gap-1',
                children: [
                  jsx('input', {
                    type: 'text',
                    value: logFilter,
                    onChange: (e) => setLogFilter(e.target.value),
                    onKeyDown: (e) => { if (e.key === 'Enter') fetchLogcat() },
                    placeholder: "filter e.g. ActivityManager:I *:S",
                    className: 'flex-1 rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-zinc-200 placeholder-zinc-500 outline-none focus:border-blue-600 font-mono text-[10px]',
                  }),
                  jsx('button', {
                    className: 'rounded border border-blue-700 bg-blue-900/50 px-2 py-1 text-blue-300',
                    onClick: fetchLogcat,
                    children: 'Apply',
                  }),
                ],
              }),

              // Logcat
              showLog && jsx('div', {
                className: 'flex-1 min-h-0 overflow-auto rounded bg-zinc-950 border border-zinc-800 p-1 font-mono text-[10px] leading-relaxed',
                children: logcat.length === 0
                  ? jsx('div', { className: 'text-zinc-600', children: 'No log output' })
                  : logcat.map((line, i) =>
                    jsx('div', {
                      key: i,
                      className: cn(
                        'whitespace-pre-wrap break-all',
                        line.includes(' E ') ? 'text-red-400' : line.includes(' W ') ? 'text-amber-400' : line.includes(' I ') ? 'text-green-400' : 'text-zinc-500'
                      ),
                      children: line,
                    })
                  ),
              }),
            ],
          }),

          // ═══════════════════════════════════════════════════════════
          // DEVICE TAB
          // ═══════════════════════════════════════════════════════════
          tab === 'device' && jsxs('div', {
            className: 'space-y-1.5',
            children: [

              // Status
              jsx('div', {
                className: cn(
                  'flex items-center gap-2 rounded px-2 py-1 border',
                  isOnline ? 'bg-green-950/30 border-green-800/40 text-green-300' : 'bg-zinc-900 border-zinc-700 text-zinc-400'
                ),
                children: jsxs('div', {
                  className: 'flex items-center gap-1.5',
                  children: [
                    jsx('span', { className: cn('h-2 w-2 rounded-full', isOnline ? 'bg-green-400 animate-pulse' : 'bg-zinc-500') }),
                    jsx('span', {
                      className: 'font-medium',
                      children: isOnline ? `Android ${status?.android_version || '?'} · SDK ${status?.sdk || '?'}` : 'Offline',
                    }),
                  ],
                }),
              }),

              // Start button (when offline)
              !isOnline && jsx('button', {
                className: 'w-full rounded border border-green-700 bg-green-900/50 py-2 text-green-300 font-medium',
                onClick: async () => {
                  haptic('tap')
                  try { await ctx.rest('/start', { method: 'POST', timeoutMs: 10000 }); host.notify({ kind: 'success', message: 'Starting emulator...' }) } catch { host.notify({ kind: 'error', message: 'Failed to start' }) }
                },
                children: '▶ Start Emulator',
              }),

              // Device Picker
              jsx('button', {
                className: 'flex items-center justify-between rounded border border-zinc-700 bg-zinc-800/50 px-2 py-1 text-zinc-300 hover:bg-zinc-700/50',
                onClick: () => setShowPicker(!showPicker),
                children: [
                  jsx('span', { children: `📱 Device: ${picker?.active_avd || 'None'}` }),
                  jsx('span', { className: 'text-zinc-500', children: showPicker ? '▲' : '▼' }),
                ],
              }),
              showPicker && jsx('div', {
                className: 'rounded border border-zinc-700 bg-zinc-900 p-2 space-y-2 max-h-[300px] overflow-y-auto',
                children: [
                  jsx('div', { className: 'text-zinc-400 font-medium', children: 'Your Devices' }),
                  ...(picker?.avds || []).map((avd) =>
                    jsx('button', {
                      key: avd.name,
                      className: cn(
                        'w-full text-left rounded border px-2 py-1.5 transition-colors',
                        avd.name === picker?.active_avd ? 'border-blue-700 bg-blue-900/30 text-blue-300' : 'border-zinc-700 bg-zinc-800 text-zinc-300 hover:bg-zinc-700'
                      ),
                      onClick: () => switchAvd(avd.name),
                      children: jsxs('div', {
                        className: 'flex justify-between items-center',
                        children: [
                          jsx('span', { className: 'font-medium', children: avd.name }),
                          jsx('span', { className: 'text-zinc-500 text-[10px]', children: avd.device || '' }),
                        ],
                      }),
                    })
                  ),
                  jsx('div', { className: 'text-zinc-400 font-medium pt-1 border-t border-zinc-800', children: 'Create New Device' }),
                  jsx('div', {
                    className: 'grid grid-cols-2 gap-1',
                    children: (picker?.devices || []).map((dev) =>
                      jsx('button', {
                        key: dev.id,
                        className: 'text-left rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-zinc-300 hover:bg-zinc-700 truncate',
                        onClick: async () => {
                          haptic('tap')
                          const name = `avd-${dev.id}`
                          try {
                            const r = await ctx.rest(`/create?name=${name}&device=${dev.id}&api=34`, { method: 'POST', timeoutMs: 60000 })
                            host.notify({ kind: r?.ok ? 'success' : 'error', message: r?.ok ? `Created ${name}!` : (r?.error || 'Failed') })
                          } catch { host.notify({ kind: 'error', message: 'Failed' }) }
                        },
                        children: dev.name || dev.id,
                      })
                    ),
                  }),
                  jsx('div', { className: 'text-zinc-400 font-medium pt-1 border-t border-zinc-800', children: 'Android Versions' }),
                  jsx('div', {
                    className: 'flex flex-wrap gap-1',
                    children: [
                      ...(picker?.installed_images || []).map((img) =>
                        jsx('span', { key: img.package, className: 'rounded-full border border-green-700 bg-green-900/30 px-2 py-0.5 text-green-300', children: `API ${img.api} ✓` })
                      ),
                      ...(picker?.available_images || []).map((img) =>
                        jsx('button', {
                          key: img.package,
                          className: 'rounded-full border border-zinc-700 bg-zinc-800 px-2 py-0.5 text-zinc-400 hover:bg-zinc-700 hover:text-zinc-200',
                          onClick: async () => {
                            haptic('tap')
                            host.notify({ kind: 'info', message: `Installing API ${img.api}...` })
                            try {
                              const r = await ctx.rest(`/create?name=api${img.api}-test&device=pixel_6&api=${img.api}`, { method: 'POST', timeoutMs: 300000 })
                              host.notify({ kind: r?.ok ? 'success' : 'error', message: r?.ok ? `API ${img.api} installed!` : (r?.error || 'Failed') })
                            } catch { host.notify({ kind: 'error', message: 'Failed' }) }
                          },
                          children: `API ${img.api}`,
                        })
                      ),
                    ],
                  }),
                  jsx('div', { className: 'text-[10px] text-zinc-600 pt-1', children: 'Green = installed · Gray = click to install' }),
                ],
              }),

              // Apps
              jsx('button', {
                className: 'flex items-center justify-between rounded border border-zinc-700 bg-zinc-800/50 px-2 py-1 text-zinc-300 hover:bg-zinc-700/50',
                onClick: () => { if (!showApps) fetchApps(); setShowApps(!showApps) },
                children: [
                  jsx('span', { children: '📦 Installed Apps' }),
                  jsx('span', { className: 'text-zinc-500', children: showApps ? '▲' : '▼' }),
                ],
              }),
              showApps && jsx('div', {
                className: 'rounded border border-zinc-700 bg-zinc-900 p-1 max-h-[260px] overflow-y-auto space-y-0.5',
                children: [
                  // Install APK (host path on the gateway machine)
                  jsx('div', {
                    key: 'install',
                    className: 'flex gap-1 px-1 py-1 border-b border-zinc-800',
                    children: [
                      jsx('input', {
                        type: 'text',
                        value: apkPath,
                        onChange: (e) => setApkPath(e.target.value),
                        placeholder: '/path/to/app.apk (gateway host)',
                        className: 'flex-1 rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-zinc-200 placeholder-zinc-500 outline-none focus:border-blue-600 font-mono text-[10px]',
                      }),
                      jsx('button', {
                        className: 'rounded border border-blue-700 bg-blue-900/50 px-2 py-1 text-blue-300 hover:bg-blue-800/50',
                        onClick: installApk,
                        children: 'Install',
                      }),
                    ],
                  }),
                  apps.length === 0
                    ? jsx('div', { key: 'empty', className: 'text-zinc-500 p-1', children: 'Loading apps...' })
                    : [
                      jsx('div', { key: 'uh', className: 'text-[10px] text-blue-400 font-medium px-2 pt-1', children: `📱 Your Apps (${apps.filter(a => a.type === 'user').length})` }),
                      ...apps.filter(a => a.type === 'user').map((app) =>
                        jsx('div', {
                          key: app.package,
                          className: 'w-full flex justify-between items-center rounded px-2 py-1 text-zinc-200 hover:bg-zinc-700',
                          children: [
                            jsx('span', {
                              className: 'truncate flex-1 text-left font-medium cursor-pointer',
                              onClick: () => launchApp(app.package),
                              children: app.label,
                            }),
                            jsx('span', {
                              className: 'text-zinc-500 text-[10px] cursor-pointer hover:text-zinc-300',
                              onClick: () => launchApp(app.package),
                              children: '▶ Launch',
                            }),
                            jsx('span', {
                              className: 'text-red-500/70 text-[10px] cursor-pointer hover:text-red-400 ml-1',
                              onClick: () => uninstallApp(app.package),
                              title: `Uninstall ${app.package}`,
                              children: '✕',
                            }),
                          ],
                        })
                      ),
                      jsx('div', { key: 'sh', className: 'text-[10px] text-zinc-500 font-medium px-2 pt-1 border-t border-zinc-800 mt-1', children: `⚙ System Apps (${apps.filter(a => a.type === 'system').length})` }),
                      ...apps.filter(a => a.type === 'system').map((app) =>
                        jsx('button', {
                          key: app.package,
                          className: 'w-full flex justify-between items-center rounded px-2 py-1 text-[10px] text-zinc-400 hover:bg-zinc-700',
                          onClick: () => launchApp(app.package),
                          children: [
                            jsx('span', { className: 'truncate flex-1 text-left', children: app.label }),
                            jsx('span', { className: 'text-zinc-600 text-[9px]', children: '▶' }),
                          ],
                        })
                      ),
                    ],
                ],
              }),

              // Advanced Tools
              jsx('button', {
                className: 'flex items-center justify-between rounded border border-zinc-700 bg-zinc-800/50 px-2 py-1 text-zinc-300 hover:bg-zinc-700/50',
                onClick: () => setShowMore(!showMore),
                children: [
                  jsx('span', { children: '⚡ Advanced Tools' }),
                  jsx('span', { className: 'text-zinc-500', children: showMore ? '▲' : '▼' }),
                ],
              }),
              showMore && jsx('div', {
                className: 'rounded border border-zinc-700 bg-zinc-900 p-2 space-y-2',
                children: [
                  jsx('div', { className: 'text-zinc-400 font-medium', children: '📍 GPS — Fake Location' }),
                  jsx('div', { className: 'text-[10px] text-zinc-500', children: 'Fake GPS for location-based testing' }),
                  jsx('div', {
                    className: 'flex gap-1',
                    children: [
                      jsx('input', { type: 'text', value: gpsLat, onChange: (e) => setGpsLat(e.target.value), placeholder: 'Latitude', className: 'flex-1 rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-zinc-200 font-mono' }),
                      jsx('input', { type: 'text', value: gpsLng, onChange: (e) => setGpsLng(e.target.value), placeholder: 'Longitude', className: 'flex-1 rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-zinc-200 font-mono' }),
                      jsx('button', { className: 'rounded border border-blue-700 bg-blue-900/50 px-3 py-1 text-blue-300', onClick: setGps, children: 'Set' }),
                      jsx('button', { className: 'rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-zinc-300', onClick: clearGps, children: 'Clear' }),
                    ],
                  }),

                  jsx('div', { className: 'text-zinc-400 font-medium pt-1 border-t border-zinc-800', children: `🔋 Battery: ${batteryLevel}%` }),
                  jsx('div', { className: 'text-[10px] text-zinc-500', children: 'Simulate low battery' }),
                  jsx('div', {
                    className: 'flex gap-1 items-center',
                    children: [
                      jsx('input', { type: 'range', min: '0', max: '100', value: batteryLevel, onChange: (e) => setBattery(parseInt(e.target.value)), className: 'flex-1' }),
                      jsx('button', { className: 'rounded border border-blue-700 bg-blue-900/50 px-2 py-1 text-blue-300', onClick: () => setBattery(batteryLevel), children: 'Set' }),
                      jsx('button', { className: 'rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-zinc-300', onClick: async () => { try { await ctx.rest('/battery/reset', { method: 'POST' }); setBatteryLevel(100) } catch {} }, children: 'Reset' }),
                      jsx('button', { className: 'rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-zinc-300', onClick: unplugBattery, children: 'Unplug' }),
                    ],
                  }),

                  jsx('div', { className: 'text-zinc-400 font-medium pt-1 border-t border-zinc-800', children: '🔗 Deep Link' }),
                  jsx('div', { className: 'text-[10px] text-zinc-500', children: 'Open a URL or app deep link' }),
                  jsx('div', {
                    className: 'flex gap-1',
                    children: [
                      jsx('input', { type: 'text', value: deeplinkUrl, onChange: (e) => setDeeplinkUrl(e.target.value), placeholder: 'myapp://path or https://...', className: 'flex-1 rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-zinc-200 font-mono' }),
                      jsx('button', { className: 'rounded border border-blue-700 bg-blue-900/50 px-3 py-1 text-blue-300', onClick: sendDeeplink, children: 'Open' }),
                    ],
                  }),

                  jsx('div', { className: 'text-zinc-400 font-medium pt-1 border-t border-zinc-800', children: '🔔 Push Notification' }),
                  jsx('div', { className: 'text-[10px] text-zinc-500', children: 'Send a test notification' }),
                  jsx('div', {
                    className: 'flex gap-1',
                    children: [
                      jsx('input', { type: 'text', value: notifTitle, onChange: (e) => setNotifTitle(e.target.value), placeholder: 'Title', className: 'flex-1 rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-zinc-200' }),
                      jsx('input', { type: 'text', value: notifBody, onChange: (e) => setNotifBody(e.target.value), placeholder: 'Message', className: 'flex-1 rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-zinc-200' }),
                      jsx('button', { className: 'rounded border border-blue-700 bg-blue-900/50 px-3 py-1 text-blue-300', onClick: sendNotification, children: 'Send' }),
                    ],
                  }),

                  jsx('div', { className: 'text-zinc-400 font-medium pt-1 border-t border-zinc-800', children: '⏺️ Screen Recording' }),
                  jsx('button', {
                    className: cn('w-full rounded border py-1.5 font-medium', isRecording ? 'border-red-700 bg-red-900/50 text-red-300' : 'border-zinc-700 bg-zinc-800 text-zinc-300'),
                    onClick: toggleRecording,
                    children: isRecording ? '⏹ Stop Recording' : '⏺ Start Recording',
                  }),
                  jsx('div', {
                    className: 'flex gap-1 pt-1',
                    children: [
                      jsx('button', {
                        className: cn('flex-1 rounded border py-1.5', isCapturing ? 'border-amber-700 bg-amber-900/50 text-amber-300' : 'border-zinc-700 bg-zinc-800 text-zinc-300'),
                        onClick: toggleCapture,
                        children: isCapturing ? '⏹ Stop Touch Capture' : '⏺ Record Touches',
                      }),
                      jsx('button', {
                        className: 'flex-1 rounded border border-zinc-700 bg-zinc-800 py-1.5 text-zinc-300',
                        onClick: playReplay,
                        children: '▶ Play Replay',
                      }),
                    ],
                  }),

                  jsx('div', { className: 'text-zinc-400 font-medium pt-1 border-t border-zinc-800', children: '🧪 Test Runner' }),
                  jsx('button', { className: 'w-full rounded border border-green-700 bg-green-900/50 py-1.5 text-green-300 font-medium', onClick: runTests, children: '▶ Run Tests' }),
                  testOutput && jsx('pre', {
                    className: 'rounded bg-zinc-950 border border-zinc-800 p-1 text-[10px] text-zinc-400 max-h-[60px] overflow-auto font-mono whitespace-pre-wrap',
                    children: testOutput,
                  }),

                  jsx('button', {
                    className: 'w-full flex justify-between items-center rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-zinc-300 hover:bg-zinc-700',
                    onClick: () => { if (!showGallery) fetchGallery(); setShowGallery(!showGallery) },
                    children: [
                      jsx('span', { children: `🖼 Screenshot Gallery (${gallery.length})` }),
                      jsx('span', { className: 'text-zinc-500', children: showGallery ? '▲' : '▼' }),
                    ],
                  }),
                  showGallery && jsx('div', {
                    className: 'grid grid-cols-3 gap-1',
                    children: gallery.length === 0
                      ? jsx('div', { className: 'text-zinc-500 col-span-3', children: 'No screenshots yet' })
                      : gallery.slice(0, 9).map((s) =>
                        jsx('div', { key: s.name, className: 'rounded border border-zinc-700 bg-zinc-800 p-1 text-[9px] text-zinc-400 truncate', children: s.name.replace('.png', '') })
                      ),
                  }),
                ],
              }),

              // Footer
              isOnline && status?.screen_size && jsx('div', {
                className: 'text-center text-[9px] text-zinc-600 mt-1',
                children: `Screen: ${status.screen_size}`,
              }),
            ],
          }),
        ],
      }),
    ],
  })
}

export default {
  id: ID,
  name: 'Android Emulator',
  defaultEnabled: true,
  register(ctx) {
    ctx.registerMany([
      {
        id: 'pane',
        area: 'panes',
        title: 'Emulator',
        data: { placement: 'right', width: '300px' },
        render: () => jsx(EmulatorPane, { ctx }),
      },
    ])
  },
}
