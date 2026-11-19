import assert from 'node:assert/strict'
import { chromium } from 'playwright'

const url = process.argv[2]
if (!url) throw new Error('Pass the HTTP URL of the rehearsal board.')
const origin = new URL(url).origin
const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE })
const page = await browser.newPage({ viewport: { width: 1360, height: 900 } })
page.setDefaultTimeout(30_000)
const errors = []
page.on('pageerror', error => errors.push(error.message))
await page.route('**/*', route => {
  const target = new URL(route.request().url())
  return target.protocol.startsWith('http') && target.origin !== origin
    ? route.abort()
    : route.continue()
})
const button = name => page.getByRole('button', { name })
const visibleText = text => page.getByText(text, { exact: true }).first().waitFor()

try {
  await page.goto(url, { waitUntil: 'domcontentloaded' })
  await page.getByRole('heading', { name: 'Draft sequence' }).waitFor()
  const branches = page.locator('.branch-node')
  await branches.filter({ hasText: 'Poppy' }).first().click()
  await visibleText('protected')
  await branches.filter({ hasText: 'Vi' }).first().click()
  await visibleText('still possible')
  await branches.filter({ hasText: 'Poppy' }).last().click()
  await visibleText('conflict')
  await button('Compare branches').click()
  const comparison = page.getByRole('dialog', { name: 'Compare branches' })
  await comparison.locator('.compare-lane').first().waitFor()
  assert.equal(await comparison.locator('.compare-lane').count(), 2)
  await page.keyboard.press('Escape')

  await button('Coach brief').click()
  const note = 'Use Maokai if the opponent commits Poppy.'
  await page.getByLabel('Coach notes').fill(note)
  await button('Save brief').click()
  await visibleText('Saved to this branch')
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.getByRole('heading', { name: 'Draft sequence' }).waitFor()
  await button('Coach brief').click()
  assert.equal(await page.getByLabel('Coach notes').inputValue(), note)

  await button('Match prep').click()
  await button(/Historical replay.*Cloud9 vs Dignitas/i).click()
  await button('Create rehearsal').click()
  await visibleText('Historical replay')
  const replay = button(/Replay historical/).first()
  await replay.waitFor()
  // One of the two teams owns the first turn. The other gets the second turn.
  const mass = page.locator('.outlook-panel .mass-key')
  if (!await mass.count()) {
    await replay.click()
    await visibleText('TURN 02')
  }
  await mass.waitFor()
  assert.match(await mass.innerText(), /residual/i)
  await button('Match prep').click()
  await button(/New matchup/).click()
  await page.getByLabel('Team 1', { exact: true }).fill('Northstar')
  await page.getByLabel('Team 2', { exact: true }).fill('Open Circuit')
  await page.getByLabel('Your team', { exact: true }).selectOption('1')
  await page.getByLabel('First pick', { exact: true }).selectOption('0')
  await page.getByLabel('As-of date').fill('2026-10-03')
  await page.getByLabel('Patch', { exact: true }).selectOption('16.17')
  await page.getByLabel('Series format').selectOption('Bo3')
  await button('Start Game 1').click()
  await page.getByRole('heading', { name: 'Northstar ban' }).waitFor()
  assert.equal(await page.locator('.slot-filled').count(), 0)
  assert.equal(await page.locator('.game-tab').count(), 3)
  await page.setViewportSize({ width: 320, height: 800 })
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
  assert.deepEqual(errors, [])
  console.log('Verified branch legality, coach-note persistence, public outlook and mobile layout.')
} finally {
  await browser.close()
}
