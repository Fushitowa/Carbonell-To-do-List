import { supabase, supabaseConfigured } from './supabase.js';

const taskTable = 'tasks';
const apiBase = (import.meta.env.VITE_TASK_API_URL || 'http://127.0.0.1:8001').replace(/\/$/, '');
const apiStartAttempts = 100;

async function apiRequest(path, options = {}) {
  const response = await fetch(`${apiBase}${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  });
  let payload = null;
  try { payload = await response.json(); } catch { /* The API may return an empty body. */ }
  if (!response.ok) throw new Error(payload?.detail || `Local API returned HTTP ${response.status}.`);
  return payload;
}

function toLocalDateTime(value) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return '';
  const pad = (number) => String(number).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function fromDatabase(row) {
  return {
    id: row.id,
    title: row.title,
    done: Boolean(row.done),
    createdAt: Date.parse(row.created_at) || Date.now(),
    dueAt: row.due_at ? toLocalDateTime(row.due_at) : '',
    reminded: Boolean(row.reminded),
    notes: typeof row.notes === 'string' ? row.notes : '',
    color: typeof row.color === 'string' ? row.color : '',
  };
}

function toDatabase(task, userId) {
  return {
    id: task.id,
    owner_id: userId,
    title: task.title,
    done: Boolean(task.done),
    created_at: new Date(task.createdAt).toISOString(),
    due_at: task.dueAt ? new Date(task.dueAt).toISOString() : null,
    reminded: Boolean(task.reminded),
    notes: task.notes || '',
    color: task.color || null,
  };
}

export async function loadCloudTasks() {
  const { data: sessionData, error: sessionError } = await supabase.auth.getSession();
  if (sessionError) throw sessionError;

  let user = sessionData.session?.user;
  if (!user) {
    const { data, error } = await supabase.auth.signInAnonymously();
    if (error) throw error;
    user = data.user;
  }
  if (!user) throw new Error('Supabase did not return a user session.');

  const { data, error } = await supabase
    .from(taskTable)
    .select('id, owner_id, title, done, created_at, due_at, reminded, notes, color')
    .eq('owner_id', user.id)
    .order('created_at', { ascending: false });
  if (error) throw error;

  return { userId: user.id, tasks: (data || []).map(fromDatabase) };
}

export async function saveCloudTasks(tasks, userId, previousIds) {
  const currentIds = new Set(tasks.map((task) => task.id));
  const rows = tasks.map((task) => toDatabase(task, userId));

  if (rows.length) {
    const { error } = await supabase.from(taskTable).upsert(rows, { onConflict: 'id' });
    if (error) throw error;
  }

  const deletedIds = [...previousIds].filter((id) => !currentIds.has(id));
  if (deletedIds.length) {
    const { error } = await supabase
      .from(taskTable)
      .delete()
      .eq('owner_id', userId)
      .in('id', deletedIds);
    if (error) throw error;
  }

  return currentIds;
}

export async function loadTasksFromStore() {
  if (supabaseConfigured) {
    const result = await loadCloudTasks();
    return { mode: 'supabase', userId: result.userId, tasks: result.tasks };
  }

  let lastError;
  for (let attempt = 0; attempt < apiStartAttempts; attempt += 1) {
    try {
      await apiRequest('/health');
      const tasks = await apiRequest('/api/tasks');
      return { mode: 'local-api', userId: null, tasks: Array.isArray(tasks) ? tasks : [] };
    } catch (error) {
      lastError = error;
      if (attempt < apiStartAttempts - 1) await new Promise((resolve) => window.setTimeout(resolve, 1000));
    }
  }
  throw new Error(`Local API did not become ready after ${apiStartAttempts} seconds. ${lastError?.message || ''}`);
}

export async function syncTasksToStore(tasks, store, previousIds) {
  if (store.mode === 'supabase') {
    return saveCloudTasks(tasks, store.userId, previousIds);
  }

  await apiRequest('/api/tasks/sync', {
    method: 'PUT',
    body: JSON.stringify(tasks),
  });
  return new Set(tasks.map((task) => task.id));
}
