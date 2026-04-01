import express from 'express';
import cors from 'cors';
import fs from 'fs';
import path from 'path';

const app = express();
const PORT = 3001;
const STATE_DIR = path.resolve(import.meta.dirname, '../../state');
const LOGS_DIR = path.resolve(import.meta.dirname, '../../logs');

app.use(cors());
app.use(express.json());

function readJson(filePath: string, fallback: unknown = []) {
  try {
    const raw = fs.readFileSync(filePath, 'utf-8');
    return JSON.parse(raw);
  } catch {
    return fallback;
  }
}

function readText(filePath: string): string {
  try {
    return fs.readFileSync(filePath, 'utf-8');
  } catch {
    return '';
  }
}

function readJsonl(filePath: string, limit = 100): unknown[] {
  try {
    const raw = fs.readFileSync(filePath, 'utf-8');
    const lines = raw.trim().split('\n').filter(Boolean);
    const parsed = lines.map(line => {
      try { return JSON.parse(line); } catch { return null; }
    }).filter(Boolean);
    return parsed.slice(-limit).reverse();
  } catch {
    return [];
  }
}

function atomicWrite(filePath: string, data: unknown) {
  const tmp = filePath + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2));
  fs.renameSync(tmp, filePath);
}

// GET endpoints
app.get('/api/state', (_req, res) => res.json(readJson(path.join(STATE_DIR, 'agent_state.json'), {})));
app.get('/api/ledger', (_req, res) => res.json(readJson(path.join(STATE_DIR, 'ledger.json'))));
app.get('/api/journal', (_req, res) => res.json({ content: readText(path.join(STATE_DIR, 'journal.md')) }));
app.get('/api/conversations', (_req, res) => res.json(readJson(path.join(STATE_DIR, 'conversations.json'))));
app.get('/api/inbox', (_req, res) => res.json(readJson(path.join(STATE_DIR, 'inbox.json'))));
app.get('/api/pipeline', (_req, res) => res.json(readJson(path.join(STATE_DIR, 'pipeline.json'))));
app.get('/api/projections', (_req, res) => res.json(readJson(path.join(STATE_DIR, 'projections.json'))));
app.get('/api/proposals', (_req, res) => res.json(readJson(path.join(STATE_DIR, 'proposals.json'))));
app.get('/api/ui-requests', (_req, res) => res.json(readJson(path.join(STATE_DIR, 'ui_requests.json'))));
app.get('/api/watches', (_req, res) => res.json(readJson(path.join(STATE_DIR, 'watches.json'))));
app.get('/api/audits', (_req, res) => res.json(readJson(path.join(STATE_DIR, 'audits.json'))));
app.get('/api/costs', (_req, res) => res.json(readJson(path.join(STATE_DIR, 'api_costs.json'))));
app.get('/api/events', (_req, res) => res.json(readJsonl(path.join(LOGS_DIR, 'events.jsonl'))));
app.get('/api/memory', (_req, res) => res.json(readJson(path.join(STATE_DIR, 'memory.json'), {})));

// POST: send message to agent inbox
app.post('/api/inbox', (req, res) => {
  const { message } = req.body;
  if (!message) return res.status(400).json({ error: 'message required' });

  const inboxPath = path.join(STATE_DIR, 'inbox.json');
  const inbox = readJson(inboxPath) as unknown[];
  inbox.push({ timestamp: new Date().toISOString(), content: message });
  atomicWrite(inboxPath, inbox);
  res.json({ ok: true });
});

// POST: approve/reject proposal
app.post('/api/proposals/:id/review', (req, res) => {
  const id = parseInt(req.params.id);
  const { status, feedback } = req.body;
  if (!['approved', 'rejected'].includes(status)) {
    return res.status(400).json({ error: 'status must be approved or rejected' });
  }

  const proposalsPath = path.join(STATE_DIR, 'proposals.json');
  const proposals = readJson(proposalsPath) as { id: number; status: string; feedback: string; resolved_at: string }[];
  const proposal = proposals.find(p => p.id === id);
  if (!proposal) return res.status(404).json({ error: 'proposal not found' });

  proposal.status = status;
  proposal.feedback = feedback || '';
  proposal.resolved_at = new Date().toISOString();
  atomicWrite(proposalsPath, proposals);
  res.json({ ok: true });
});

app.listen(PORT, () => {
  console.log(`Hustle Agent API running on http://localhost:${PORT}`);
});
