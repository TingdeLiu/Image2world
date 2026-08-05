/**
 * Outbound links that appear in more than one place in the interface.
 *
 * These are kept together because they are easy to get wrong in ways nobody
 * notices locally: the help link pointed at the upstream project this viewer
 * was ported from until it turned up in a public deployment, sending visitors
 * to someone else's repository.
 */

export const REPO_URL = 'https://github.com/TingdeLiu/Image2world'

/**
 * World Labs' API key page. The query string is their own campaign tagging, so
 * signups arriving from this app are attributable on their side.
 */
export const MARBLE_KEYS_URL =
  'https://platform.worldlabs.ai/api-keys?utm_source=marble_web&utm_medium=product_cta&utm_campaign=api_cta&utm_content=help_menu_api_cta'
