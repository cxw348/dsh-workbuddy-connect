/**
 * Pool CLI commands: bulk import, listing, check-in, and enable/disable.
 *
 * Kept out of `bin.ts` so the existing doctor/status/logout paths stay
 * untouched — the pool is additive, and a regression in it must not be able to
 * break sign-in diagnostics.
 *
 * Every command here is *non-interactive by construction*: a batch tool that
 * prompts is a batch tool that cannot run in a pipeline. Anything ambiguous (an
 * unreadable file, a malformed line) is reported per input and the exit code says
 * whether anything failed.
 *
 * @module dsh-workbuddy-connect/pool-cli
 */

import { readFileSync, readdirSync, statSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { basename, extname, join, resolve } from 'node:path'
import { resolveDshHome } from '@deepseek-ai/dsh-home-paths'
import {
  parseImportText,
  poolKeyOf,
  WorkBuddyAccountPool,
  type WorkBuddyCheckinStatus,
  type WorkBuddyPoolCredential,
  type WorkBuddyPoolEntry,
} from './account-pool.ts'
import {
  atRestKeyProviderFor,
  classifyDesktopAuthDocument,
  keyIdsOf,
  openAuthField,
  unwrapDesktopAuthDocument,
  type WorkBuddyAtRestKeyProvider,
} from './desktop-credential-protection.ts'
import { CN_VARIANT, variantFor, WORKBUDDY_VARIANTS, type WorkBuddyVariant } from './variants.ts'
import { regionOf, WorkBuddyUpstreamClient } from './upstream.ts'

/** Pool subcommands, so the CLI can dispatch on its first argument. */
export const POOL_ACTIONS: ReadonlySet<string> = new Set([
  'accounts',
  'checkin',
  'clear',
  'disable',
  'enable',
  'import',
  'remove',
  'use',
])

/**
 * Import file extensions this reader will open when given a directory.
 *
 * `.info` is the one that matters in practice: it is what the WorkBuddy desktop
 * app names every credential file, including the timestamped backups it leaves
 * on each sign-in and account switch. Leaving it out made a directory import
 * scan past every real credential and report "no account document found" — the
 * difference between the feature working on the user's actual machine and not.
 */
const IMPORT_EXTENSIONS: ReadonlySet<string> = new Set(['.json', '.jsonl', '.ndjson', '.txt', '.auth', '.info'])

/** A stable, non-reversible digest used for anonymous pool keys. */
export function tokenHash(seed: string): string {
  return createHash('sha256').update(seed).digest('hex')
}

/** Build one variant's pool. */
export function makePool(path?: string): WorkBuddyAccountPool {
  return new WorkBuddyAccountPool({
    ...path === undefined ? {} : { path },
    hashOf: tokenHash,
  })
}

/**
 * The default pool file for one variant.
 *
 * One file per variant, matching how every other per-variant store in this bundle
 * names itself: the CN and international endpoints share account ids but never
 * credentials, and one pool for both would hand a CN account to the international
 * provider.
 */
export function defaultPoolPath(variant: WorkBuddyVariant): string {
  return join(
    resolveDshHome(),
    variant.id === CN_VARIANT.id ? '.workbuddy-pool.json' : '.workbuddy-ai-pool.json',
  )
}

/** One command's resolved options. */
interface PoolOptions {
  json: boolean
  replace: boolean
  credits: boolean
}

/** Collect the files one import path refers to. */
export function expandImportPaths(inputs: readonly string[]): {
  files: string[]
  errors: { path: string; reason: string }[]
} {
  const files: string[] = []
  const errors: { path: string; reason: string }[] = []
  for (const input of inputs) {
    const absolute = resolve(input)
    let stats
    try {
      stats = statSync(absolute)
    } catch (error: unknown) {
      errors.push({ path: input, reason: `cannot read: ${error instanceof Error ? error.message : String(error)}` })
      continue
    }
    if (stats.isDirectory()) {
      let names: string[]
      try {
        names = readdirSync(absolute)
      } catch (error: unknown) {
        errors.push({ path: input, reason: `cannot list: ${error instanceof Error ? error.message : String(error)}` })
        continue
      }
      // Sorted so a directory import is reproducible run to run.
      for (const name of names.sort()) {
        if (!IMPORT_EXTENSIONS.has(extname(name).toLowerCase())) continue
        files.push(join(absolute, name))
      }
      continue
    }
    files.push(absolute)
  }
  return { files, errors }
}

/**
 * Read one import file, decrypting it when it is a WorkBuddy 5.6+ envelope.
 *
 * This is the path a real user takes: the desktop app seals its token fields
 * since 5.6, so the *only* account a fresh install has is an encrypted document.
 * An importer that understood plaintext alone would report "no account document
 * found" for the user's own sign-in — which is exactly the shape of "I imported
 * my account and it still says one account".
 *
 * The decryption reuses the plugin's existing classification and field-opening
 * helpers rather than reimplementing the envelope, so the pool and the live
 * credential store can never disagree about how a document reads. A document
 * that is neither plaintext nor a decodable envelope is reported as such, and a
 * failed unlock names the field and key id — never token content.
 */
async function readImportFile(
  file: string,
  keyProvider: WorkBuddyAtRestKeyProvider | undefined,
): Promise<{ text: string, encrypted: boolean }> {
  const text = readFileSync(file, 'utf8')
  const classification = classifyDesktopAuthDocument(text)
  if (classification.format === 'plaintext') return { text, encrypted: false }
  if (classification.format !== 'encrypted') return { text, encrypted: false }
  if (keyProvider === undefined) {
    throw new Error('the file is an encrypted WorkBuddy credential and no key provider is available for this provider id')
  }
  const key = await keyProvider.protectorKeyFor(keyIdsOf(classification.wrapped.fields))
  const plaintext = unwrapDesktopAuthDocument(classification, (field) => {
    const opened = openAuthField(key, field.envelope)
    if (opened === undefined) {
      throw new Error(`the encrypted credential's ${field.field} could not be decrypted (envelope key id ${field.envelope.keyId})`)
    }
    return opened
  })
  return { text: plaintext, encrypted: true }
}

/**
 * Keep only the credentials that belong to this variant's region.
 *
 * The two desktop apps share one auth directory and differ only by filename, so
 * pointing `import` at that directory — the natural thing to do, and the only
 * way to find the timestamped per-account backups — hands a mixed batch to
 * whichever pool is being filled. A CN token sent to the international endpoint
 * (or the reverse) leaks one product's credential to the other, which is exactly
 * what the single-account store already refuses to do.
 *
 * Rejecting at import time is the only place this can be caught usefully: the
 * alternative is a pool that looks populated and fails every request from the
 * mismatched accounts.
 */
export function partitionByRegion(
  accounts: readonly WorkBuddyPoolCredential[],
  variant: WorkBuddyVariant,
): { accepted: WorkBuddyPoolCredential[], rejected: WorkBuddyPoolCredential[] } {
  const accepted: WorkBuddyPoolCredential[] = []
  const rejected: WorkBuddyPoolCredential[] = []
  for (const account of accounts) {
    if (regionOf(account.domain) === variant.region) accepted.push(account)
    else rejected.push(account)
  }
  return { accepted, rejected }
}

/**
 * Whether a filename belongs to the *other* product's credential family.
 *
 * Used to skip before decryption rather than after, and that ordering matters for
 * two reasons beyond tidiness: opening a credential spawns the product's Electron
 * to fetch its at-rest key, so decrypting a file this pool can never use is a
 * wasted process launch per file; and the other product's key does not open these
 * files at all, so the attempt fails and reports an unlock error for a file that
 * was never this pool's business.
 *
 * The two families are distinguishable by name alone — the desktop app writes
 * `workbuddy-desktop*.info` for CN and `workbuddy-desktop-ai*.info` for the
 * international product, with the same timestamped suffixes on backups.
 */
export function belongsToOtherProduct(file: string, variant: WorkBuddyVariant): boolean {
  const name = basename(file)
  if (!name.startsWith('workbuddy-desktop')) return false
  const isInternationalFile = name.startsWith('workbuddy-desktop-ai')
  return variant.region === 'cn' ? isInternationalFile : !isInternationalFile
}

/**
 * Read and parse every import file, returning the accounts found.
 *
 * A file that yields zero accounts is an error rather than a silent no-op: the
 * most likely cause is pointing at the wrong file, and "imported 0 accounts" with
 * exit 0 would let that mistake stand.
 */
export async function collectAccounts(
  files: readonly string[],
  keyProvider?: WorkBuddyAtRestKeyProvider,
  variant?: WorkBuddyVariant,
): Promise<{
  accounts: WorkBuddyPoolCredential[]
  errors: { path: string; reason: string }[]
  perFile: { path: string; accounts: number; skippedLines?: number; encrypted?: boolean }[]
}> {
  const accounts: WorkBuddyPoolCredential[] = []
  const errors: { path: string; reason: string }[] = []
  const perFile: { path: string; accounts: number; skippedLines?: number; encrypted?: boolean }[] = []
  for (const file of files) {
    // The other product's credential family is skipped before any decryption
    // attempt: see `belongsToOtherProduct`.
    if (variant !== undefined && belongsToOtherProduct(file, variant)) continue
    let text: string
    let encrypted = false
    try {
      const read = await readImportFile(file, keyProvider)
      text = read.text
      encrypted = read.encrypted
    } catch (error: unknown) {
      errors.push({ path: file, reason: error instanceof Error ? error.message : String(error) })
      continue
    }
    try {
      const parsed = parseImportText(text)
      if (parsed.accounts.length === 0) {
        errors.push({ path: file, reason: 'no account document found (expected an auth document, an array, or JSON Lines)' })
        continue
      }
      accounts.push(...parsed.accounts)
      perFile.push({
        path: file,
        accounts: parsed.accounts.length,
        ...parsed.skippedLines === 0 ? {} : { skippedLines: parsed.skippedLines },
        ...encrypted ? { encrypted: true } : {},
      })
    } catch (error: unknown) {
      errors.push({ path: file, reason: error instanceof Error ? error.message : String(error) })
    }
  }
  return { accounts, errors, perFile }
}

/** Secret-free summary of one pool entry, for CLI output. */
export function describeEntry(entry: WorkBuddyPoolEntry, index: number): {
  index: number
  key: string
  nickname?: string
  domain?: string
  hasRefreshToken: boolean
  accessTokenExpiresAtMs: number
  accessTokenExpires: string
  disabled: boolean
  lastCheckinAtMs?: number
  lastCheckinStatus?: WorkBuddyCheckinStatus
} {
  return {
    index: index + 1,
    key: entry.key,
    ...entry.credential.nickname === undefined ? {} : { nickname: entry.credential.nickname },
    ...entry.credential.domain === '' ? {} : { domain: entry.credential.domain },
    hasRefreshToken: entry.credential.refreshToken !== '',
    accessTokenExpiresAtMs: entry.credential.expiresAtMs,
    accessTokenExpires: entry.credential.expiresAtMs > 0
      ? new Date(entry.credential.expiresAtMs).toISOString()
      : 'unknown',
    disabled: entry.disabled === true,
    ...entry.lastCheckinAtMs === undefined ? {} : {
      lastCheckinAtMs: entry.lastCheckinAtMs,
      lastCheckinStatus: entry.lastCheckinStatus ?? 'unknown' as WorkBuddyCheckinStatus,
    },
  }
}

/** Print a report as JSON or as the lines a human reads. */
function emit(report: unknown, jsonOutput: boolean, lines: () => string[]): void {
  if (jsonOutput) {
    process.stdout.write(`${JSON.stringify(report)}\n`)
    return
  }
  process.stdout.write([...lines(), ''].join('\n'))
}

/**
 * `import` — merge accounts from files or directories into the pool.
 *
 * `--replace` is offered because re-importing a *stale* export is a real workflow
 * (the user may have regenerated their accounts elsewhere), and the default merge
 * deliberately refuses to roll a fresher stored token back. The flag is explicit
 * so the rollback is always a decision, never a side effect.
 */
export async function commandImport(
  pool: WorkBuddyAccountPool,
  inputs: readonly string[],
  options: PoolOptions,
  variant: WorkBuddyVariant,
  keyProvider?: WorkBuddyAtRestKeyProvider,
): Promise<number> {
  if (inputs.length === 0) {
    process.stderr.write('workbuddy pool import: at least one file or directory is required\n')
    return 1
  }
  const expanded = expandImportPaths(inputs)
  const collected = await collectAccounts(expanded.files, keyProvider, variant)
  // A credential for the other product is refused, never stored: the pool is
  // keyed by variant, and one product's token must not be sent to the other's
  // endpoint. Reported rather than dropped silently, so a mixed directory is
  // visibly a mixed directory.
  const { accepted, rejected } = partitionByRegion(collected.accounts, variant)
  const errors = [...expanded.errors, ...collected.errors]
  const regionNotes = rejected.length === 0 ? [] : [
    `skipped ${rejected.length} account(s) belonging to ${variant.region === 'cn' ? 'WorkBuddy AI' : 'WorkBuddy (CN)'} (this pool serves ${variant.displayName}): ${rejected.map(account => account.uid === '' ? '(no uid)' : account.uid).join(', ')}`,
  ]
  if (accepted.length === 0) {
    const report = {
      action: 'import',
      pool: pool.filePath(),
      files: collected.perFile,
      errors,
      ...regionNotes.length === 0 ? {} : { regionSkipped: rejected.length },
      imported: 0,
      added: 0,
      updated: 0,
      skipped: 0,
      counts: pool.counts(),
    }
    emit(report, options.json, () => [
      `Pool: ${pool.filePath()}`,
      ...errors.map(error => `ERROR ${error.path}: ${error.reason}`),
      ...regionNotes,
      'No accounts imported.',
    ])
    return 1
  }
  // Deduplicate *within* the import batch too: two files carrying the same
  // account must not both win. The freshest document wins by the same
  // strictly-newer rule `merge` applies, so a stale copy listed later in a
  // directory cannot override a current one; on a tie the first file listed wins,
  // which keeps a directory import deterministic.
  const deduped = new Map<string, WorkBuddyPoolCredential>()
  for (const account of accepted) {
    const key = poolKeyOf(account, tokenHash)
    const previous = deduped.get(key)
    if (previous === undefined || account.expiresAtMs > previous.expiresAtMs) deduped.set(key, account)
  }
  if (options.replace) pool.clear()
  const merged = pool.merge([...deduped.values()])
  const counts = pool.counts()
  const report = {
    action: 'import',
    pool: pool.filePath(),
    files: collected.perFile,
    errors,
    ...regionNotes.length === 0 ? {} : { regionSkipped: rejected.length },
    imported: deduped.size,
    added: merged.added,
    updated: merged.updated,
    skipped: merged.skipped,
    entries: merged.entries,
    counts,
    ...options.replace ? { replaced: true } : {},
  }
  emit(report, options.json, () => [
    `Pool: ${pool.filePath()}`,
    ...collected.perFile.map(file =>
      `Read ${file.accounts} account(s) from ${file.path}${file.encrypted === true ? ' (decrypted)' : ''}${file.skippedLines === undefined ? '' : ` (${file.skippedLines} line(s) without an account)`}`),
    ...errors.map(error => `ERROR ${error.path}: ${error.reason}`),
    ...regionNotes,
    ...merged.entries.map(entry => `  ${entry.action.padEnd(8)} ${entry.key}${entry.nickname === undefined ? '' : ` (${entry.nickname})`}`),
    '',
    `Imported ${deduped.size} account(s): added=${merged.added} updated=${merged.updated} unchanged=${merged.skipped}`,
    `Pool now holds ${counts.total} account(s), ${counts.usable} usable.`,
  ])
  return errors.length > 0 ? 1 : 0
}

/** `accounts` — list the pool, secret-free. */
export function commandAccounts(pool: WorkBuddyAccountPool, options: PoolOptions): number {
  const entries = pool.all()
  const counts = pool.counts()
  const report = {
    action: 'accounts',
    pool: pool.filePath(),
    counts,
    accounts: entries.map(describeEntry),
  }
  emit(report, options.json, () => entries.length === 0
    ? [
        `Pool: ${pool.filePath()}`,
        'No accounts in the pool; the plugin is using the desktop app\'s sign-in.',
      ]
    : [
        `Pool: ${pool.filePath()}`,
        `${counts.total} account(s), ${counts.usable} usable`,
        '',
        ...entries.map((entry, index) => {
          const described = describeEntry(entry, index)
          return [
            `${String(described.index).padStart(3)}. ${described.key}`,
            described.nickname === undefined ? '' : ` (${described.nickname})`,
            described.disabled ? ' [disabled]' : '',
            `\n     expires ${described.accessTokenExpires}${described.hasRefreshToken ? '' : ' (no refresh token)'}`,
            described.lastCheckinStatus === undefined
              ? ''
              : `\n     last check-in ${described.lastCheckinStatus} at ${new Date(described.lastCheckinAtMs!).toISOString()}`,
          ].join('')
        }),
      ])
  return 0
}

/**
 * `checkin` — claim the daily grant for every usable account.
 *
 * Sequential on purpose. The accounts share one upstream origin, and a burst of
 * parallel calls from one machine is the pattern most likely to be rate-limited —
 * which would make the batch report a wall of transport failures and hide which
 * accounts are actually fine.
 */
export async function commandCheckin(
  pool: WorkBuddyAccountPool,
  options: PoolOptions,
  client: WorkBuddyUpstreamClient = new WorkBuddyUpstreamClient(),
): Promise<number> {
  const entries = pool.usable()
  const rows: {
    key: string
    nickname?: string
    status: WorkBuddyCheckinStatus
    detail: string
    credits?: number
  }[] = []
  let ok = 0
  let already = 0
  let failed = 0
  let auth = 0
  for (const entry of entries) {
    const outcome = await client.dailyCheckin({ ...entry.credential, source: 'dsh' })
    pool.markCheckin(entry.key, outcome.status)
    if (outcome.status === 'ok') ok += 1
    else if (outcome.status === 'already') already += 1
    else if (outcome.status === 'auth') auth += 1
    else failed += 1
    const row: (typeof rows)[number] = {
      key: entry.key,
      ...entry.credential.nickname === undefined ? {} : { nickname: entry.credential.nickname },
      status: outcome.status,
      detail: outcome.detail,
    }
    if (options.credits && outcome.status !== 'auth') {
      try {
        const fetched = await client.fetchCredits({ ...entry.credential, source: 'dsh' })
        row.credits = fetched.total
      } catch {}
    }
    rows.push(row)
  }
  const report = {
    action: 'checkin',
    pool: pool.filePath(),
    total: entries.length,
    ok,
    already,
    failed,
    authInvalid: auth,
    results: rows,
  }
  emit(report, options.json, () => entries.length === 0
    ? [
        `Pool: ${pool.filePath()}`,
        'No usable accounts in the pool; nothing to check in.',
      ]
    : [
        `Pool: ${pool.filePath()}`,
        '',
        'key                                  | status  | credits | detail',
        '-------------------------------------+---------+---------+------------------------------',
        ...rows.map(row => [
          row.key.length > 36 ? row.key.slice(0, 36) : row.key.padEnd(36),
          '|',
          row.status.padEnd(7),
          '|',
          (row.credits === undefined ? '-' : String(row.credits)).padEnd(7),
          '|',
          row.detail.replace(/\s+/gu, ' ').slice(0, 40),
        ].join(' ')),
        '',
        `total=${entries.length} ok=${ok} already=${already} failed=${failed} auth_invalid=${auth}`,
      ])
  // A dead session is the one outcome a human must act on, so it is part of the
  // exit code; a transient failure is not, because a cron job should not page
  // anyone for one flaky request.
  return auth > 0 ? 2 : failed > 0 ? 1 : 0
}

/** Move one entry to the front of the pool, so rotation reaches it first. */
export function moveToFront(pool: WorkBuddyAccountPool, key: string): boolean {
  const entries = pool.all() as WorkBuddyPoolEntry[]
  const index = entries.findIndex(entry => entry.key === key)
  if (index <= 0) return index === 0
  const [entry] = entries.splice(index, 1)
  entries.unshift(entry!)
  pool.persist()
  return true
}

/** `use` / `disable` / `enable` / `remove` — mutate one entry by key. */
export function commandEntry(pool: WorkBuddyAccountPool, action: string, key: string, options: PoolOptions): number {
  const entry = pool.get(key)
  if (entry === undefined) {
    process.stderr.write(`workbuddy pool ${action}: no account with key ${JSON.stringify(key)}\n`)
    return 1
  }
  let changed: boolean
  switch (action) {
    case 'disable':
      changed = pool.setDisabled(key, true)
      break
    case 'enable':
      changed = pool.setDisabled(key, false)
      break
    case 'remove':
      changed = pool.remove(key)
      break
    default:
      // `use` moves the account to the front by rotating the array: the cursor is
      // positional, so "make this the active account" is an ordering change, not
      // a stored flag.
      changed = moveToFront(pool, key)
      break
  }
  const counts = pool.counts()
  const report = { action, key, changed, counts }
  emit(report, options.json, () => [
    `${action}: ${changed ? 'ok' : 'no change'} — ${key}`,
    `Pool now holds ${counts.total} account(s), ${counts.usable} usable.`,
  ])
  return changed ? 0 : 1
}

/** `clear` — empty the pool. */
export function commandClear(pool: WorkBuddyAccountPool, options: PoolOptions): number {
  const counts = pool.counts()
  pool.clear()
  emit(
    { action: 'clear', removed: counts.total, counts: pool.counts() },
    options.json,
    () => [`Cleared ${counts.total} account(s); the plugin falls back to the desktop app's sign-in.`],
  )
  return 0
}

/**
 * Parse and execute one pool subcommand; returns the process exit code.
 *
 * All argument handling lives here rather than in the CLI entry point so that
 * entry point needs one import and one dispatch line — the pool is additive, and
 * the smaller its footprint in the existing file, the smaller the chance a future
 * upstream change collides with it.
 */
export async function runPoolCommand(argv: readonly string[]): Promise<number> {
  const [action, ...flags] = argv
  let providerId: string | undefined
  let poolPath: string | undefined
  let jsonOutput = false
  let replace = false
  let credits = false
  const positional: string[] = []
  for (let index = 0; index < flags.length; index += 1) {
    const flag = flags[index]!
    if (flag === '--json') {
      jsonOutput = true
      continue
    }
    if (flag === '--replace') {
      replace = true
      continue
    }
    if (flag === '--credits') {
      credits = true
      continue
    }
    if (flag === '--provider' || flag === '--pool') {
      const value = flags[index + 1]
      if (value === undefined || value.startsWith('--')) {
        process.stderr.write(`dsh-workbuddy-connect: ${flag} requires a value\n`)
        return 1
      }
      if (flag === '--provider') providerId = value
      else poolPath = value
      index += 1
      continue
    }
    if (flag.startsWith('--provider=')) {
      providerId = flag.slice('--provider='.length)
      continue
    }
    if (flag.startsWith('--pool=')) {
      poolPath = flag.slice('--pool='.length)
      continue
    }
    if (flag.startsWith('-')) {
      process.stderr.write(`dsh-workbuddy-connect: unknown option for pool ${action}: ${flag}\n`)
      return 1
    }
    positional.push(flag)
  }
  const variant = providerId === undefined ? CN_VARIANT : variantFor(providerId)
  if (variant === undefined) {
    process.stderr.write(
      `dsh-workbuddy-connect: unknown provider ${JSON.stringify(providerId)}; expected one of ${WORKBUDDY_VARIANTS.map(v => v.id).join(', ')}\n`,
    )
    return 1
  }
  const pool = makePool(poolPath ?? defaultPoolPath(variant))
  const options: PoolOptions = { json: jsonOutput, replace, credits }
  // Only these four take a key argument; anything else with positional words is a
  // mistake worth naming rather than ignoring.
  if (['disable', 'enable', 'remove', 'use'].includes(action!)) {
    if (positional.length !== 1) {
      process.stderr.write(`dsh-workbuddy-connect: pool ${action} takes exactly one account key (see: dsh-workbuddy-connect accounts)\n`)
      return 1
    }
    return commandEntry(pool, action!, positional[0]!, options)
  }
  if (['accounts', 'clear'].includes(action!) && positional.length > 0) {
    process.stderr.write(`dsh-workbuddy-connect: pool ${action} takes no arguments; got ${positional.join(' ')}\n`)
    return 1
  }
  try {
    switch (action) {
      case 'import':
        // The key provider is built from the same shared helper the plugin host
        // uses, so `import` unlocks a desktop credential exactly the way the
        // live store does — and a failure to locate the helper is reported as
        // the same diagnosis `doctor` gives.
        return await commandImport(pool, positional, options, variant, atRestKeyProviderFor(variant))
      case 'accounts':
        return commandAccounts(pool, options)
      case 'checkin':
        return await commandCheckin(pool, options)
      case 'clear':
        return commandClear(pool, options)
    }
  } catch (error: unknown) {
    process.stderr.write(`dsh-workbuddy-connect: pool ${action} failed: ${error instanceof Error ? error.message : String(error)}\n`)
    return 1
  }
  return 1
}
