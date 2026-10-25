# Supabase setup

Supabase is an optional hosted storage mode. By default, the app uses the local PostgreSQL, Backend, and API services described in [`desktop_control_panel/README.md`](desktop_control_panel/README.md). When both Supabase variables are set, the app uses Supabase Postgres instead and signs in anonymously, so each browser profile gets a private Supabase user whose task rows are protected by row-level security.

1. Create a Supabase project.
2. In **Authentication → Sign In / Providers**, enable **Anonymous Sign-Ins**.
3. Open the project's SQL Editor and run [`supabase/schema.sql`](supabase/schema.sql).
4. Copy `.env.example` to `.env.local`. Set `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY` from the project's API settings. The key can be the publishable key or the legacy anon key; never use a `service_role` or secret key in this browser app.
5. Restart the Vite development server. When the browser first connects, existing local tasks are copied to Supabase if that anonymous user's cloud task list is empty.

Anonymous sign-in keeps data associated with the current browser profile. This version does not include email/password accounts or cross-device account linking.

See Supabase's docs for [anonymous sign-ins](https://supabase.com/docs/reference/javascript/auth-signinanonymously) and [row-level security](https://supabase.com/docs/guides/database/postgres/row-level-security).
