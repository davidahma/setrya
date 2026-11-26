// A disposable application. Run on a fresh database with isolated test credentials.
import { closeSync, openSync } from 'node:fs';
import { createServer } from 'node:http';
import { DatabaseSync } from 'node:sqlite';

const { SETRYA_TEST_DB, SETRYA_SUBJECT_REF, SETRYA_READER_TOKEN, SETRYA_REVIEWER_TOKEN,
  SETRYA_ERASER_TOKEN, SETRYA_FIXTURE_MODE } = process.env;
const tokens = [SETRYA_READER_TOKEN, SETRYA_REVIEWER_TOKEN, SETRYA_ERASER_TOKEN];
if (!SETRYA_TEST_DB || !/^[a-f0-9]{64}$/.test(SETRYA_SUBJECT_REF ?? '') ||
    tokens.some(token => !token) || new Set(tokens).size !== tokens.length ||
    !['guarded', 'broken'].includes(SETRYA_FIXTURE_MODE ?? '')) {
  throw Error('Set the fixture mode, fresh database path, synthetic subject and three distinct test tokens.');
}

const conversation = 'ticket-204';
type Question = "What was the customer's depot code?" | 'Which depot handled this ticket?' | 'When is dispatch?';
const questions = new Map<Question, string>([
  ["What was the customer's depot code?", 'cached-note'],
  ['Which depot handled this ticket?', 'cached-note'],
  ['When is dispatch?', 'dispatch'],
]);
const readers = new Map([
  [`Bearer ${SETRYA_READER_TOKEN}`, 'duty-agent'],
  [`Bearer ${SETRYA_REVIEWER_TOKEN}`, 'duty-reviewer'],
]);

closeSync(openSync(SETRYA_TEST_DB, 'wx', 0o600));
const db = new DatabaseSync(SETRYA_TEST_DB);
db.exec('CREATE TABLE notes (id TEXT PRIMARY KEY, ticket TEXT, subject TEXT, body TEXT)');
const insert = db.prepare('INSERT INTO notes VALUES (?, ?, ?, ?)');
insert.run('note', conversation, SETRYA_SUBJECT_REF!, 'Harbor-17');
insert.run('cached-note', conversation, SETRYA_SUBJECT_REF!, 'Harbor-17');
insert.run('dispatch', conversation, 'team', 'Thursday');
const read = db.prepare('SELECT body FROM notes WHERE ticket = ? AND id = ?');
const erase = db.prepare(SETRYA_FIXTURE_MODE === 'broken'
  ? "DELETE FROM notes WHERE ticket = ? AND subject = ? AND id = 'note'"
  : 'DELETE FROM notes WHERE ticket = ? AND subject = ?');

const server = createServer(async (req, res) => {
  const answer = req.method === 'POST' && req.url === '/answer';
  const deletion = req.method === 'DELETE' && req.url === '/erasure';
  if (!answer && !deletion) return res.writeHead(404).end();
  const caller = readers.get(req.headers.authorization ?? '');
  const mayErase = req.headers.authorization === `Bearer ${SETRYA_ERASER_TOKEN}`;
  if ((answer && !caller) || (deletion && !mayErase)) return res.writeHead(403).end();
  try {
    const chunks: Buffer[] = [];
    let bytes = 0;
    for await (const chunk of req.iterator({ destroyOnReturn: false })) {
      bytes += chunk.length;
      if (bytes > 8192) return res.writeHead(413, { connection: 'close' }).end();
      chunks.push(chunk);
    }
    const input = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    if (input.conversation !== conversation) return res.writeHead(404).end();
    if (deletion) {
      if (input.subject_ref !== SETRYA_SUBJECT_REF) return res.writeHead(404).end();
      erase.run(conversation, input.subject_ref);
      return res.writeHead(204).end();
    }
    const question: Question = input.question;
    const key = questions.get(question);
    if (!key) return res.writeHead(400).end();
    const body = read.get(conversation, key)?.body ?? 'No matching note.';
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ caller, route: '/answer', conversation, question, text: body }));
  } catch {
    res.writeHead(400).end();
  }
});

server.listen(0, '127.0.0.1', () => {
  const address = server.address();
  if (address && typeof address === 'object') console.log(`http://127.0.0.1:${address.port}`);
});
