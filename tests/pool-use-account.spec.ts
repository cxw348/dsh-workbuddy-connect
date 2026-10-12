/**
 * End-to-end: press Use on one account and confirm the active credential changes.
 *
 * Renders the real card, mounts the real control handler on a real HTTP server,
 * and drives the request the way the browser does — so the guards (loopback,
 * the per-process key) and the host's switch logic are all exercised rather
 * than stubbed.
 */
import { mkdtempSync, rmSync } from 'node:fs'
import type { IncomingMessage, ServerResponse } from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createElement } from 'react'
import { act, create, type ReactTestRenderer } from 'react-test-renderer'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { WorkBuddyAccountPool } from '../src/account-pool.ts'
import { WorkBuddyPoolCredentialResolver } from '../src/pool-store.ts'
import { WorkBuddyProbeRouteOptions, createProbeKey, workBuddyProbeHandler } from '../src/probe-route.ts'
import { AI_CARD_VARIANT, WorkBuddyPluginCard } from '../src/client/WorkBuddyPluginCard.tsx'
import { en } from '../src/client/locales.ts'

const t = (key: keyof typeof en, params: Record<string, unknown> = {}): string =>
  Object.entries(params).reduce(
    (text, [name, value]) => text.replace(`{${name}}`, String(value)),
    en[key] as string,
  )

const dirs: string[] = []
function tempDir(): string {
  const dir = mkdtempSync(join(tmpdir(), 'wb-use-e2e-'))
  dirs.push(dir)
  return dir
}

afterEach(() => {
  for (const dir of dirs.splice(0)) rmSync(dir, { recursive: true, force: true })
  vi.unstubAllGlobals()
})

function credential(uid: string, token: string) {
  return {
    accessToken: token,
    refreshToken: `RT-${token}`,
    expiresAtMs: Date.now() + 3_600_000,
    domain: 'workbuddy.ai',
    uid,
  }
}

describe('pressing Use on an account switches the active credential', () => {
  it('changes which credential the pool serves', async () => {
    const dir = tempDir()
    const pool = new WorkBuddyAccountPool({ path: join(dir, 'pool.json') })
    pool.merge([credential('u-1', 'AT-ONE'), credential('u-2', 'AT-TWO'), credential('u-3', 'AT-THREE')])

    const fallback = {
      current: async () => undefined,
      resolve: async () => { throw new Error('no fallback') },
      status: async () => ({ state: 'signed-out' as const }),
      logout: async () => {},
      setDesktopPath: () => {},
      desktopAuthPath: () => undefined,
      ownAuthPath: () => '',
      desktopAuthFormat: async () => 'absent' as const,
      resolvedDesktopAuthPath: async () => undefined,
      desktopFilePresent: async () => false,
    }
    const store = new WorkBuddyPoolCredentialResolver({
      pool,
      fallback,
      refresh: async () => ({ accessToken: 'x' }),
    })

    // The real control route, with the same guards production uses.
    const key = createProbeKey()
    const options: WorkBuddyProbeRouteOptions = {
      probe: async () => ({ state: 'ok' }),
      clear: () => {},
      useAccount: async account => {
        // The host-side path: `preferAccount` persists the reorder and points
        // the resolver at the account in one call.
        if (store.preferAccount(account) !== true) {
          return { state: 'refused', reason: `account ${account} is not usable in this pool` }
        }
        const credential = await store.current()
        return { state: 'ok', reason: `now using ${credential?.uid ?? '?'}` }
      },
    }
    // A stand-in origin for the headers the card and the guard both use; no
    // socket is opened, the handler is invoked in-process.
    const port = 0

    try {
      // Before: the first account is what the pool serves.
      expect((await store.current())!.accessToken).toBe('AT-ONE')

      // The card, talking to the real route as the browser would.
      const section = store.poolSection()!
      let view: ReactTestRenderer | undefined
      const statusBody = {
        status: 'signed-in',
        nickname: 'one',
        expiresAt: Date.now() + 3_600_000,
        probeKey: key,
        credits: { total: 350, accounts: [] },
        models: [],
        pool: section,
        probe: { consent: false, running: false, candidates: [], results: [] },
      }
      // In-process rather than over HTTP: the mock answers a control POST by
      // invoking the very handler the route would, so the guards, the payload
      // parse, and the host's switch logic are all exercised without a socket.
      // Forwarding over the network would also race the fetch stub, which is
      // what makes this test fail only when it runs alongside others.
      const request = vi.fn().mockImplementation(async (_url: string, init?: RequestInit) => {
        if (init?.method !== 'POST') return { ok: true, json: async () => statusBody }
        const probeHeaders: Record<string, string | string[] | undefined> = {
          host: `127.0.0.1:${port}`,
          origin: `http://127.0.0.1:${port}`,
          'x-workbuddy-probe-key': key,
        }
        let answer: { status: number; body: unknown } = { status: 599, body: { error: 'no reply' } }
        const pending = new Promise<void>(resolve => {
          void workBuddyProbeHandler(options, key)(
            {
              method: 'POST',
              headers: probeHeaders,
              // A minimal readable stream of the body the card sent.
              [Symbol.asyncIterator]: async function* () {
                yield Buffer.from(String(init.body ?? ''), 'utf8')
              },
            } as unknown as IncomingMessage,
            {
              writeHead(status: number) {
                answer = { ...answer, status }
              },
              end(payload?: string) {
                if (payload !== undefined) answer = { ...answer, body: JSON.parse(payload) }
                resolve()
              },
            } as unknown as ServerResponse,
          )
        })
        await pending
        return { ok: answer.status >= 200 && answer.status < 300, json: async () => answer.body }
      })
      vi.stubGlobal('fetch', request)
      vi.stubGlobal('window', {
        setInterval: () => 1,
        clearInterval: () => {},
        addEventListener: () => {},
        removeEventListener: () => {},
        localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
      })

      await act(async () => {
        view = create(createElement(WorkBuddyPluginCard, { variant: AI_CARD_VARIANT, t }))
      })
      await act(async () => {
        view!.root.findAllByType('button')[0]!.props.onClick()
      })

      const useButtons = view!.root.findAll(node =>
        node.type === 'button' && node.children.join('') === en.poolUseAccount,
      )
      // Rows 2 and 3 are switchable.
      expect(useButtons).toHaveLength(2)

      await act(async () => {
        // The first switchable row is the second account.
        useButtons[0]!.props.onClick()
      })

      // The switch is settled by the control request the card made, so wait for
      // it rather than racing the microtask queue.
      await vi.waitFor(async () => {
        expect((await store.current())!.accessToken).toBe('AT-TWO')
      }, { timeout: 3000, interval: 10 })

      // The switch really happened at the host.
      expect((await store.current())!.accessToken).toBe('AT-TWO')
      // And it survived on disk, so a restart keeps the choice.
      const reloaded = new WorkBuddyAccountPool({ path: join(dir, 'pool.json') })
      expect(reloaded.all()[0]!.credential.accessToken).toBe('AT-TWO')

      act(() => view?.unmount())
    } finally {
      // Nothing to tear down: the route ran in this process.
    }
  })
})
