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

async function completeDraft() {
  for (let turn = 1; turn <= 20; turn++) {
    await visibleText(`TURN ${String(turn).padStart(2, '0')}`)
    const pass = button('Pass ban')
    if (await pass.count()) {
      await pass.click()
    } else {
      await button('Custom champion').click()
      const dialog = page.getByRole('dialog', { name: 'Choose a legal champion' })
      await dialog.locator('.champion-option').first().click()
      await dialog.locator('button[type=submit]').click()
    }
  }
  await page.getByLabel('Game winner').waitFor()
}

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
  for (const game of [2, 3]) {
    const badge = page.locator('.decision-header .turn-badge')
    await badge.waitFor()
    const start = Number((await badge.innerText()).split(' ')[1])
    for (let turn = start; turn <= 20; turn++) {
      await visibleText(`TURN ${String(turn).padStart(2, '0')}`)
      await replay.click()
    }
    await button('Reveal historical result').waitFor()
    assert.equal(await button('Record result').count(), 0)
    await button('Reveal historical result').click()
    if (game === 2) await button('Start historical Game 3').click()
  }
  await page.getByRole('heading', { name: 'Cloud9 won the series' }).waitFor()
  assert.match(await page.locator('.series-score').innerText(), /Cloud9 2.*Dignitas 1/s)
  await button('Match prep').click()
  await button(/New matchup/).click()
  await page.getByLabel('Team 1', { exact: true }).fill('Northstar')
  await page.getByLabel('Team 2', { exact: true }).fill('Open Circuit')
  await page.getByLabel('Your team', { exact: true }).selectOption('1')
  await page.getByLabel('First pick', { exact: true }).selectOption('0')
  await page.getByLabel('As-of date').fill('2026-10-03')
  await page.getByLabel('Patch', { exact: true }).selectOption('16.17')
  await page.getByLabel('Series format').selectOption('Bo3')
  await page.getByLabel('Side and pick order').selectOption('first_selection')
  await page.getByLabel('Draft format').selectOption('standard')
  await button('Start Game 1').click()
  await page.getByRole('heading', { name: 'Northstar ban' }).waitFor()
  assert.equal(await page.locator('.slot-filled').count(), 0)
  assert.equal(await page.locator('.game-tab').count(), 3)
  await button('Standard rules').click()
  await page.getByRole('dialog', { name: 'Standard draft rules' }).waitFor()
  await page.keyboard.press('Escape')
  await page.getByLabel('Blue map side').selectOption('Northstar')
  await page.locator('.matrix-row.side-blue').filter({ hasText: 'Northstar' }).waitFor()
  await page.getByLabel('First pick team').selectOption('Open Circuit')
  await page.getByRole('heading', { name: 'Open Circuit ban' }).waitFor()
  assert.match(await page.locator('.matrix-row.side-red').innerText(), /FIRST PICK/)
  await page.getByText('Champion eligibility', { exact: true }).click()
  await page.getByLabel('Additional organizer disables').fill('Poppy')
  await button('Apply restrictions').click()
  await visibleText('Champion eligibility · 1 disabled')
  await button('Custom champion').click()
  await page.getByLabel('Search champions').fill('Poppy')
  await visibleText('No legal champions match your search.')
  await button('Close dialog').click()
  await completeDraft()
  await page.getByLabel('Game winner').selectOption('Northstar')
  await button('Record result').click()
  await page.getByLabel('Open Circuit chose').selectOption('map_side')
  await page.getByLabel('Game 2 first pick').selectOption('Open Circuit')
  await page.getByLabel('Game 2 blue side').selectOption('Northstar')
  await page.getByLabel('Additional organizer disables').fill('')
  await button('Start Game 2').click()
  await completeDraft()
  await page.getByLabel('Game winner').selectOption('Northstar')
  await button('Record result').click()
  await page.getByRole('heading', { name: 'Northstar won the series' }).waitFor()
  assert.equal(await button('Start Game 3').count(), 0)
  await page.getByLabel('Explore hypothetical Game 3').check()
  await page.getByLabel('Game 3 first pick').selectOption('Northstar')
  await button('Rehearse Game 3').click()
  await visibleText('Hypothetical branch')
  assert.match(await page.locator('.series-score').innerText(), /Northstar 2.*Open Circuit 0/s)
  await page.setViewportSize({ width: 320, height: 800 })
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
  assert.deepEqual(errors, [])
  console.log('Verified branches, coach notes, outlook, independent selections, game eligibility, Standard draft, confirmed clinch, hypothetical game and mobile layout.')
} finally {
  await browser.close()
}
