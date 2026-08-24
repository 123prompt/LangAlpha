'use strict'

const http = require('node:http')
const { shell } = require('electron')
const origins = require('./origins')
const { navigate } = require('./navigate')
const { forDisplay } = require('./redact')

// ---------------------------------------------------------------------------
// System-browser OAuth (RFC 8252), intercepted in the shell.
//
// Google returns disallowed_useragent for OAuth inside an embedded webview
// (confirmed live; a user-agent spoof was tried and failed), so the authorize
// URL has to open in the user's real browser and the code has to come back over
// a loopback listener.
//
// The shell does this without either SPA knowing. It catches the authorize
// navigation, swaps redirect_to for the loopback, and when the code arrives
// sends the window that started the flow to the redirect_to it had ORIGINALLY
// asked for, with the code appended. That lands on each app's own callback
// route, where @supabase/ssr (flowType pkce, detectSessionInUrl on) redeems it
// against the verifier already sitting in that renderer's cookie jar.
//
// INVARIANT: the exchange happens in the renderer, never here. The PKCE
// verifier never leaves the cookie jar that created it, which is also what makes
// the loopback hop safe: anything listening on that port gets a code it cannot
// redeem.
// ---------------------------------------------------------------------------

// Every port here must be in the Supabase Redirect URLs allowlist, because
// Supabase matches redirect_to as an exact string.
const CALLBACK_PORTS = [8788, 8789, 8790]
const FLOW_TIMEOUT_MS = 5 * 60_000

let callbackPort = null
let pending = null
let server = null

/**
 * Supabase's authorize endpoint, matched by shape rather than by host: the shell
 * is not told which Supabase project the web build points at, and a self-hoster
 * brings their own.
 */
function isAuthorizeUrl(url) {
  try {
    const u = new URL(url)
    return (
      (u.protocol === 'https:' || u.protocol === 'http:') &&
      u.pathname.endsWith('/auth/v1/authorize') &&
      u.searchParams.has('redirect_to')
    )
  } catch {
    return false
  }
}

function donePage(ok, detail) {
  const title = ok ? 'Signed in' : 'Sign-in failed'
  const body = ok
    ? 'You can close this tab and return to LangAlpha.'
    : detail || 'Something went wrong.'
  return `<!doctype html><meta charset="utf-8"><title>${title}</title>
  <body style="margin:0;display:grid;place-items:center;height:100vh;background:#191919;color:#e8e8e8;
               font:15px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
    <div style="text-align:center"><h1 style="font-size:19px;margin:0 0 8px">${escapeHtml(title)}</h1>
    <p style="margin:0;opacity:.65">${escapeHtml(body)}</p></div></body>`
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ))
}

/**
 * Did a browser NAVIGATE here, rather than fetch this as part of some other page?
 *
 * The provider returns by sending the user's browser to this URL, which every
 * browser labels `document`. Any other value is a page on the open web reaching
 * a loopback port as a subresource, and `<img src="http://127.0.0.1:8788/callback
 * ?error=x">` is the whole attack: no-cors means the attacker never has to read
 * the reply, because the damage is the side effect. The flow is consumed and the
 * window that was signing in is driven to a failure it never had.
 *
 * Requiring a parameter the attacker also supplies cannot separate the two; how
 * the request was made can. Absent is allowed and must stay allowed: that is
 * curl, a non-browser client, or a browser too old to send the header, and a web
 * page cannot suppress it from inside a browser that does.
 *
 * This does not address a hostile process on the same machine, which can send
 * anything. Nothing observable at this port can, which is why the PKCE invariant
 * above is what actually bounds that case.
 */
function isProviderNavigation(headers) {
  const dest = headers['sec-fetch-dest']
  return !dest || dest === 'document'
}

function handleCallback(req, res) {
  const url = new URL(req.url, `http://127.0.0.1:${callbackPort}`)
  const code = url.searchParams.get('code')
  const error = url.searchParams.get('error_description') || url.searchParams.get('error')

  // Carrying neither parameter is what disqualifies a request, not just the
  // path: the provider always comes back with one or the other, and anything on
  // this machine can reach a loopback port. A favicon fetch, a probe, or the URL
  // left in a browser tab would otherwise consume the flow the real callback is
  // still on its way to complete, and send the window to a 'sign-in failed' for
  // a sign-in that was fine.
  if (url.pathname !== '/callback' || (!code && !error)) {
    res.writeHead(404)
    return res.end()
  }

  if (!isProviderNavigation(req.headers)) {
    console.warn(`[auth] ignoring a /callback fetched as '${req.headers['sec-fetch-dest']}'`)
    res.writeHead(404)
    return res.end()
  }

  const answer = (ok, detail) => {
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
    res.end(donePage(ok, detail))
  }

  // Answered after the flow is resolved rather than before it. A code that
  // arrives with nothing waiting is discarded, and writing the page first told
  // that tab "Signed in" while the app was told nothing at all: the two surfaces
  // the user is looking at disagreed, and the browser was the one that lied.
  //
  // Nothing waiting is not only a stray request. Two flows started from one
  // window are indistinguishable here, because the callback carries no flow
  // identity, so the first code to arrive consumes the slot and the second finds
  // it empty. The renderer's PKCE verifier is single-slot too, so only the newest
  // flow could have redeemed anyway; the honest thing is to say the sign-in did
  // not land and let the user start one that can.
  if (!pending) {
    console.warn('[auth] callback arrived with nothing waiting')
    return answer(false, 'This sign-in is no longer the one in progress. Return to LangAlpha and start it again.')
  }
  const flow = pending
  pending = null
  clearTimeout(flow.timer)
  answer(!!code && !error, error)
  flow.finish({ code, error })
}

function startCallbackServer(i = 0) {
  return new Promise((resolve) => {
    if (i >= CALLBACK_PORTS.length) {
      console.error('[auth] no free callback port in', CALLBACK_PORTS)
      return resolve(null)
    }
    const srv = http.createServer(handleCallback)
    const nextPort = () => resolve(startCallbackServer(i + 1))
    srv.once('error', nextPort)
    srv.listen(CALLBACK_PORTS[i], '127.0.0.1', () => {
      // Only a *bind* failure means "try the next port". Left attached, a later
      // socket error would open a second listener and repoint `callbackPort`
      // while the authorize URL already in the browser still names this one, so
      // the provider's redirect arrives at a port nobody is reading.
      srv.removeListener('error', nextPort)
      // Replaced, never just removed: a listening server with no 'error'
      // listener *throws* on the next socket error, which ends the app.
      srv.on('error', (err) => console.error(`[auth] callback server: ${err.code || err.message}`))
      server = srv
      callbackPort = CALLBACK_PORTS[i]
      console.log(`[auth] callback listening on http://127.0.0.1:${callbackPort}/callback`)
      resolve(callbackPort)
    })
  })
}

/**
 * Release the port and drop any flow waiting on it. No listener means no way for
 * a code to arrive, so a flow left pending could only ever time out.
 */
function stopCallbackServer() {
  if (pending) {
    clearTimeout(pending.timer)
    pending = null
  }
  if (!server) return
  server.close()
  server = null
  callbackPort = null
}

/** Append a query param to a URL that may already carry one. */
function withParam(rawUrl, key, value) {
  const u = new URL(rawUrl)
  u.searchParams.set(key, value)
  return u.toString()
}

/**
 * Take over an authorize navigation. `win` is the window that tried to make it,
 * and the one the code is handed back to.
 *
 * Returns false when the flow is not ours to take, in which case the caller must
 * let the navigation proceed normally.
 */
function begin(rawUrl, win) {
  const authorize = new URL(rawUrl)
  const originalRedirect = authorize.searchParams.get('redirect_to')

  // The source, not only the destination. `isAuthorizeUrl` is host-agnostic on
  // purpose, so a self-hoster can bring their own Supabase project, and
  // `setWindowOpenHandler` sends every `window.open` through here. Without this,
  // anything the shell renders could hand us an authorize URL on a host of its
  // choosing and have the flow claimed: superseding a sign-in already in flight,
  // or steering this window to a path of its choosing on our own origin, since
  // `isOurs` answers for the origin and not the path. A real sign-in click only
  // ever comes from a page we serve.
  const from = win.isDestroyed() ? '' : win.webContents.getURL()
  if (!origins.isOurs(from)) {
    console.warn(`[auth] an authorize navigation from '${forDisplay(from)}' is not ours to take`)
    return false
  }

  // Only redirect back into an app we own. Without this the shell would happily
  // drive its own window to wherever a crafted authorize URL pointed, using a
  // code the user just authorized. Asked first, because everything below claims
  // the navigation and a flow that is not ours has to stay unclaimed.
  if (!origins.isOurs(originalRedirect)) {
    console.warn(`[auth] redirect_to '${originalRedirect}' is not one of ours; not intercepting`)
    return false
  }

  // Ours, and unserviceable. Declining here would let the navigation proceed as
  // 'external', which opens the authorize URL in the system browser: the flow
  // then completes into a browser profile holding none of the PKCE verifier this
  // renderer just minted, so it cannot be redeemed and the window is never told
  // why. Three ports is not a lot, and a preview plus a packaged app plus one
  // other local service is enough to take them all. Claim it and say so.
  if (!callbackPort) {
    console.error('[auth] no loopback listener; refusing the flow rather than sending it somewhere it cannot finish')
    // Claimed either way: a window that closed under us still must not have its
    // authorize URL handed to a browser that cannot finish it.
    if (win.isDestroyed()) return true
    navigate(win, withParam(originalRedirect, 'error',
      'Sign-in could not start: the local callback port is in use. Quit any other copy of LangAlpha and try again.'))
    return true
  }

  if (pending) {
    clearTimeout(pending.timer)
    // A second flow from the SAME window is the user clicking sign-in again,
    // not an outcome to report. That window has not moved, so `finish` would
    // drive it to the app's callback route showing a sign-in failure while the
    // browser flow they just started is still running. The new flow owns it.
    if (pending.win !== win) pending.finish({ error: 'superseded by another sign-in' })
    pending = null
  }

  authorize.searchParams.set('redirect_to', `http://127.0.0.1:${callbackPort}/callback`)

  // Where the window was when the flow started, so the two paths that end a flow
  // without the user asking can tell "still waiting to sign in" from "gave up
  // and went back to work". Turns here run for minutes.
  const startedAt = win.webContents.getURL()

  const finish = ({ code, error }) => {
    if (win.isDestroyed()) return
    // A code means the user just completed a sign-in and is coming back for the
    // result, so that lands wherever they are. An error is the shell's own
    // timer or a second flow talking, and neither is worth throwing away a page
    // the user moved on to: five minutes is shorter than a research turn, and
    // `superseded` needs no network round trip at all, so any page in the shell
    // could force a navigation just by starting two flows.
    if (!code && win.webContents.getURL() !== startedAt) {
      console.warn(`[auth] dropping '${error}': the window has moved on`)
      return
    }
    win.show()
    win.focus()
    // Hand the result to the app's own callback route either way: it already
    // renders a signing-in state and knows where to go next, and an error shown
    // in the app beats a dead-end page in the browser.
    const target = code
      ? withParam(originalRedirect, 'code', code)
      : withParam(originalRedirect, 'error', error || 'sign-in failed')
    navigate(win, target)
  }

  pending = {
    finish,
    // Which window this flow belongs to, so a second one from the same window
    // can be told apart from a second one somewhere else.
    win,
    timer: setTimeout(() => {
      if (!pending) return
      const flow = pending
      pending = null
      flow.finish({ error: 'timed out after 5 minutes' })
    }, FLOW_TIMEOUT_MS),
  }

  console.log('[auth] handing the authorize URL to the system browser')
  const flow = pending
  shell.openExternal(authorize.toString()).catch((err) => {
    console.error(`[auth] the system browser refused the authorize URL: ${err.message}`)
    // Nothing is ever coming back: no browser opened, so no callback will. The
    // in-app navigation was already prevented on the strength of this flow
    // starting, so leaving it pending is five minutes of a window that silently
    // refused to go anywhere, ending in a timeout that blames the user's wait.
    if (pending !== flow) return
    clearTimeout(flow.timer)
    pending = null
    flow.finish({ error: 'could not open your browser' })
  })
  return true
}

module.exports = {
  isAuthorizeUrl, begin, startCallbackServer, stopCallbackServer,
  isProviderNavigation, CALLBACK_PORTS,
}
