'use strict';
const element = id => document.getElementById(id);
const telegram = window.Telegram?.WebApp;
const parameters = new URLSearchParams(window.location.search);
const job = parameters.get('job');
const revision = Number(parameters.get('revision'));
const fields = ['allow_comment', 'allow_duet', 'allow_stitch', 'commercial_content',
  'brand_organic_toggle', 'brand_content_toggle', 'is_aigc', 'consent'];
const labels = { PUBLIC_TO_EVERYONE: 'Everyone', MUTUAL_FOLLOW_FRIENDS: 'Friends',
  FOLLOWER_OF_CREATOR: 'Followers', SELF_ONLY: 'Only me' };
let submitting = false;
let submitted = false;
async function api(method, body) {
  const response = await fetch(`/publisher/api/tiktok/${job}?revision=${revision}`, {
    method, headers: { 'X-Telegram-Init-Data': telegram.initData, 'Content-Type': 'application/json' },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  const text = await response.text();
  let result;
  try { result = JSON.parse(text); } catch { result = {error: text}; }
  if (!response.ok) throw new Error(result.error || 'Request failed. Reopen the form from the bot.');
  return result;
}
function update() {
  const commercial = element('commercial_content').checked;
  if (!commercial) {
    element('brand_organic_toggle').checked = false;
    element('brand_content_toggle').checked = false;
  }
  element('brands').hidden = !commercial;
  const branded = element('brand_content_toggle').checked;
  element('brand_content_toggle').disabled = element('privacy').value === 'SELF_ONLY';
  for (const option of element('privacy').options) {
    option.disabled = option.dataset.unavailable === 'true' || (branded && option.value === 'SELF_ONLY');
  }
  const ownBrand = element('brand_organic_toggle').checked;
  element('branded_consent').hidden = !branded;
  element('brand_label').textContent = branded ? 'Your video will be labeled Paid partnership.'
    : ownBrand ? 'Your video will be labeled Promotional content.' : '';
  let problem = '';
  if (commercial && !branded && !ownBrand) problem = 'Choose Your brand, Branded content, or both.';
  if (branded && element('privacy').value === 'SELF_ONLY') problem = 'Branded content cannot use Only me.';
  element('validation').textContent = problem;
  element('publish').disabled = submitting || submitted || !!problem || !element('privacy').value
    || !element('caption').value.trim() || !element('consent').checked;
}
element('post').addEventListener('input', event => {
  // Changing post settings requires consent to the final content and disclosures.
  if (event.target !== element('consent')) element('consent').checked = false;
  update();
});
element('post').addEventListener('submit', async event => {
  event.preventDefault();
  if (element('publish').disabled) return;
  submitting = true; update();
  const values = {revision, caption: element('caption').value, privacy_level: element('privacy').value};
  for (const field of fields) values[field] = element(field).checked;
  try {
    const result = await api('POST', values);
    submitted = true;
    element('status').textContent = result.message;
    element('post').hidden = true;
  } catch (error) { element('status').textContent = error.message; }
  finally { submitting = false; update(); }
});
(async () => {
  try {
    if (!telegram?.initData || !/^\d+$/.test(job || '') || !Number.isInteger(revision)) {
      throw new Error('Open this form using Accept in your private bot chat.');
    }
    telegram.ready();
    const context = await api('GET');
    element('account').textContent = `Posting to ${context.creator.creator_nickname}`;
    element('title').textContent = context.title;
    element('caption').value = context.caption;
    element('video').src = context.video_url;
    for (const privacy of context.creator.privacy_level_options) {
      const option = new Option(labels[privacy], privacy);
      option.disabled = context.private_test && privacy !== 'SELF_ONLY';
      option.dataset.unavailable = String(option.disabled);
      element('privacy').add(option);
    }
    for (const interaction of ['comment', 'duet', 'stitch']) {
      const disabled = context.creator[`${interaction}_disabled`];
      element(`allow_${interaction}`).disabled = disabled;
      element(`${interaction}_hint`).textContent = disabled ? '(disabled in TikTok)' : '';
    }
    element('restriction').textContent = context.private_test
      ? 'Private testing: only Only me is available, and your TikTok account must be private.' : '';
    element('post').hidden = false;
    element('status').textContent = '';
    update();
  } catch (error) { element('status').textContent = error.message; }
})();
