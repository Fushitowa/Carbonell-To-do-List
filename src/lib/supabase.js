import { createClient } from '@supabase/supabase-js';

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL?.trim();
const supabaseKey = import.meta.env.VITE_SUPABASE_ANON_KEY?.trim();

export const supabaseConfigurationError = Boolean(supabaseUrl) !== Boolean(supabaseKey)
  ? 'Set both VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY in .env.local.'
  : '';

export const supabaseConfigured = Boolean(supabaseUrl && supabaseKey);

export const supabase = supabaseConfigured
  ? createClient(supabaseUrl, supabaseKey)
  : null;
