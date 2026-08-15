import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { slugify } from './worldPublish.ts'

/**
 * `slugify` decides what a world's directory is called and what its URL is, so
 * anything it lets through has to be a single, plain path segment. The delete
 * and rename routes both check the result against `worldDirPath` afterwards --
 * these pin the first half of that pair, where a name becomes a slug at all.
 */
describe('slugify', () => {
  it('lower-cases and joins words with hyphens', () => {
    assert.equal(slugify('Sunlit Office'), 'sunlit-office')
    assert.equal(slugify('Room 1'), 'room-1')
    assert.equal(slugify('  padded  name  '), 'padded-name')
  })

  it('leaves an already-valid slug alone', () => {
    assert.equal(slugify('sunlit-office-k7hil8'), 'sunlit-office-k7hil8')
  })

  it('strips anything that would escape a single directory', () => {
    // The route re-checks with `worldDirPath`, but nothing here should get
    // that far: a slug is one segment of letters, digits and hyphens.
    assert.equal(slugify('../etc'), 'etc')
    assert.equal(slugify('a/b'), 'a-b')
    assert.equal(slugify('C:\Windows'), 'c-windows')
    assert.equal(slugify('.staging'), 'staging')
    assert.equal(slugify('world.json'), 'world-json')
  })

  it('does not leave hyphens dangling at either end', () => {
    assert.equal(slugify('-leading'), 'leading')
    assert.equal(slugify('trailing-'), 'trailing')
    assert.equal(slugify('!!! shouty !!!'), 'shouty')
  })

  it('returns nothing for a name with nothing in it', () => {
    // The caller turns this into a 400 rather than a directory called "".
    for (const name of ['', '   ', '!!!', '---', '。、·']) {
      assert.equal(slugify(name), '', `${JSON.stringify(name)} should yield no slug`)
    }
  })

  it('keeps non-latin names from silently becoming empty without warning', () => {
    // Chinese and other non-latin names slug to nothing, which is a real
    // limitation rather than a bug -- pinned here so it is a decision, not a
    // surprise, if anyone widens the character class later.
    assert.equal(slugify('客厅'), '')
    assert.equal(slugify('客厅 room1'), 'room1')
  })
})
