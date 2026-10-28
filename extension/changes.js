// Chronicle Export — what counts as changed since the last export.
// Loaded by the content script (Fetch) and the background (the toolbar badge),
// so both count the same conversations. Function declarations only: the
// content script can be injected into a tab twice, and a repeated top-level
// const would throw.

// Personal accounts can belong to more than one org; the one that chats is ours.
function pickChatOrg(orgs) {
  const chatOrg = orgs.find((o) => Array.isArray(o.capabilities) && o.capabilities.includes('chat'));
  return (chatOrg || orgs[0]).uuid;
}

// seen maps a conversation's uuid to the updated_at it had when last downloaded.
function isUnchanged(conv, seen) {
  const prev = seen[conv.uuid];
  return Boolean(prev && conv.updated_at && conv.updated_at <= prev);
}
