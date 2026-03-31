export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  const CHATWORK_TOKEN = process.env.CHATWORK_API_TOKEN;
  const ROOM_ID = '306672911';

  if (!CHATWORK_TOKEN) {
    return res.status(500).json({ error: 'ChatWork API token not configured' });
  }

  try {
    const data = req.body;

    const lines = [
      '[info][title]GTMタグ設置依頼[/title]',
      `依頼者: ${data.requester}`,
      `代理店/運用者: ${data.agency}`,
      `媒体: ${data.platform}`,
      `タグ名: ${data.tagName}`,
      `タグ種別: ${data.tagType}`,
      `作業種別: ${data.workType}`,
      `発火ページ: ${(data.pageTypes || []).join(', ')}`,
      `対象LP (${(data.lpNames || []).length}本): ${(data.lpNames || []).join(', ')}`,
      `派生LP: ${data.includeVariants}`,
      `設置希望日: ${data.desiredDate}`,
      data.notes ? `補足: ${data.notes}` : '',
      `依頼日: ${data.requestDate}`,
      '[/info]'
    ].filter(Boolean);

    const message = lines.join('\n');

    const response = await fetch(
      `https://api.chatwork.com/v2/rooms/${ROOM_ID}/messages`,
      {
        method: 'POST',
        headers: {
          'X-ChatWorkToken': CHATWORK_TOKEN,
          'Content-Type': 'application/x-www-form-urlencoded',
        },
        body: `body=${encodeURIComponent(message)}`,
      }
    );

    if (!response.ok) {
      const errorText = await response.text();
      return res.status(response.status).json({ error: errorText });
    }

    const result = await response.json();
    return res.status(200).json({ success: true, messageId: result.message_id });
  } catch (error) {
    return res.status(500).json({ error: error.message });
  }
}
