/**
 * Does the settings card actually render the account pool?
 *
 * The pool shipped CLI-only first, which is exactly the failure this file
 * guards against: the host emitted a `pool` field, the strings existed in the
 * bundle, and the card still showed nothing because nothing rendered them.
 * "The field is on the wire" and "the user can see it" are different claims, so
 * this renders the real card with React and asserts on the output tree.
 */
import { createElement } from 'react'
import { act, create, type ReactTestRenderer } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AI_CARD_VARIANT, WorkBuddyPluginCard } from '../src/client/WorkBuddyPluginCard.tsx'
import { en, zh } from '../src/client/locales.ts'

const t = (key: keyof typeof en, params: Record<string, unknown> = {}): string =>
  Object.entries(params).reduce(
    (text, [name, value]) => text.replace(`{${name}}`, String(value)),
    en[key] as string,
  )

/** Every string in a rendered tree, so assertions read like what a user sees. */
function textOf(view: ReactTestRenderer): string {
  const parts: string[] = []
  const walk = (node: unknown): void => {
    if (typeof node === 'string') {
      parts.push(node)
      return
    }
    if (Array.isArray(node)) {
      for (const child of node) walk(child)
      return
    }
    if (typeof node === 'object' && node !== null) {
      const element = node as { children?: unknown }
      if (element.children !== undefined) walk(element.children)
    }
  }
  walk(view.toJSON())
  return parts.join('\n')
}

describe('the card renders the account pool', () => {
  let view: ReactTestRenderer | undefined
  let statusBody: Record<string, unknown>
  const request = vi.fn()

  function status(overrides: Record<string, unknown> = {}): void {
    statusBody = {
      status: 'signed-in',
      nickname: 'one-account',
      expiresAt: Date.now() + 3_600_000,
      probeKey: 'test-key',
      credits: { total: 350, accounts: [] },
      models: [],
      probe: { consent: false, running: false, candidates: [], results: [] },
      ...overrides,
    }
  }

  beforeEach(() => {
    status()
    request.mockReset().mockImplementation(async (_url: string, init?: RequestInit) => {
      if (init?.method !== 'POST') return { ok: true, json: async () => statusBody }
      return { ok: true, json: async () => ({ state: 'ok', validation: 'non-validating', efforts: [] }) }
    })
    vi.stubGlobal('fetch', request)
    vi.stubGlobal('window', {
      setInterval: () => 1,
      clearInterval: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    })
  })

  afterEach(() => {
    // Unmount inside `act`, matching the other card tests: tearing down outside
    // it leaves React cleaning up a detached tree, which surfaces as a warning
    // about a bug that is not there.
    act(() => view?.unmount())
    view = undefined
    vi.unstubAllGlobals()
  })

  /**
   * Render the card and expand it.
   *
   * The card is collapsed by default and renders its body only when open, so a
   * test that skipped the click would assert against the header alone — and
   * would pass for the wrong reason the moment the pool block moved.
   */
  async function render(): Promise<string> {
    await act(async () => {
      view = create(createElement(WorkBuddyPluginCard, { variant: AI_CARD_VARIANT, t }))
    })
    await expand()
    return textOf(view!)
  }

  /** Click the header toggle, which is the only way the body is rendered. */
  async function expand(): Promise<void> {
    await act(async () => {
      view!.root.findAllByType('button')[0]!.props.onClick()
    })
  }

  it('shows nothing pool-related when the status carries no pool', async () => {
    // The compatibility property: a single-account install must not gain a pool
    // section it has nothing to put in.
    const text = await render()
    expect(text).not.toContain(en.poolHeading)
  })

  it('lists every account, marking the active one', async () => {
    status({
      pool: {
        total: 3,
        usable: 2,
        retired: 0,
        accounts: [
          { key: 'uid-a:', nickname: 'alpha', active: true, expiresAt: Date.now() + 86_400_000 },
          { key: 'uid-b:', nickname: 'beta', active: false, disabled: true, expiresAt: Date.now() + 86_400_000 },
          { key: 'uid-c:', active: false, expiresAt: Date.now() + 86_400_000 },
        ],
      },
    })
    const text = await render()
    // The section itself.
    expect(text).toContain(en.poolHeading)
    expect(text).toContain(t('poolSummary', { total: 3, usable: 2 }))
    // Every account, by the name a user would recognise it by.
    expect(text).toContain('alpha')
    expect(text).toContain('beta')
    expect(text).toContain('uid-c:')
    // The states that make the list worth reading.
    expect(text).toContain(en.poolActive)
    expect(text).toContain(en.poolDisabled)
  })

  it('surfaces retired counts and check-in verdicts', async () => {
    status({
      pool: {
        total: 2,
        usable: 1,
        retired: 1,
        accounts: [
          { key: 'uid-a:', active: true, lastCheckinStatus: 'already' },
          { key: 'uid-b:', active: false, lastCheckinStatus: 'auth' },
        ],
      },
    })
    const text = await render()
    expect(text).toContain(t('poolSummaryRetired', { total: 2, usable: 1, retired: 1 }))
    expect(text).toContain(en.poolCheckinAlready)
    expect(text).toContain(en.poolCheckinAuth)
  })

  it('shows an expired account as expired rather than as a date', async () => {
    status({
      pool: {
        total: 1,
        usable: 1,
        retired: 0,
        accounts: [{ key: 'uid-old:', active: true, expiresAt: Date.now() - 86_400_000 }],
      },
    })
    const text = await render()
    expect(text).toContain(en.poolAccountExpired)
  })

  it('renders the pool in the CN card too', async () => {
    // Both variants share this component, so the section must not be gated on
    // the international variant.
    status({
      pool: {
        total: 1,
        usable: 1,
        retired: 0,
        accounts: [{ key: 'cn-uid:', nickname: 'cn-user', active: true }],
      },
    })
    await act(async () => {
      // No `variant` prop at all, rather than an explicit `undefined`: the
      // project builds with exactOptionalPropertyTypes, and omitting the key is
      // how the default variant is selected.
      view = create(createElement(WorkBuddyPluginCard, { t }))
    })
    await expand()
    const text = textOf(view!)
    expect(text).toContain(en.poolHeading)
    expect(text).toContain('cn-user')
  })

  it('has a localized heading for both languages', () => {
    // A missing key would render `undefined` into the card rather than failing,
    // so the strings are asserted directly.
    expect(zh.poolHeading).toBe('账号池')
    expect(en.poolHeading).toBe('Account pool')
    expect(zh.poolActive).toBe('使用中')
    expect(zh.poolHint).toContain('import')
  })

  it('switches to a chosen account when the user presses Use', async () => {
    status({
      pool: {
        total: 3,
        usable: 3,
        retired: 0,
        accounts: [
          { key: 'uid-a:', nickname: 'alpha', active: true },
          { key: 'uid-b:', nickname: 'beta', active: false },
          { key: 'uid-c:', nickname: 'gamma', active: false },
        ],
      },
    })
    await render()
    // The button only appears on the rows that can be switched to.
    const useButtons = view!.root.findAll(node =>
      node.type === 'button' && node.children.join('') === en.poolUseAccount,
    )
    expect(useButtons).toHaveLength(2)

    await act(async () => {
      // The first switchable row is `beta`, the second `gamma`.
      useButtons[0]!.props.onClick()
    })

    const switchCalls = request.mock.calls.filter(([, init]) => {
      if (init?.method !== 'POST') return false
      try {
        return JSON.parse(String(init.body)).action === 'use-account'
      } catch {
        return false
      }
    })
    expect(switchCalls).toHaveLength(1)
    // The account asked for is the one the user pressed on.
    expect(JSON.parse(String(switchCalls[0]![1].body)).account).toBe('uid-b:')
  })

  it('offers no switch control on the active or disabled rows', async () => {
    status({
      pool: {
        total: 2,
        usable: 1,
        retired: 0,
        accounts: [
          { key: 'uid-a:', nickname: 'alpha', active: true },
          { key: 'uid-b:', nickname: 'beta', disabled: true },
        ],
      },
    })
    await render()
    const useButtons = view!.root.findAll(node =>
      node.type === 'button' && node.children.join('') === en.poolUseAccount,
    )
    // Neither the active row (already in use) nor the disabled one (the host
    // refuses it) can be switched to, so neither offers the button.
    expect(useButtons).toHaveLength(0)
  })
})
