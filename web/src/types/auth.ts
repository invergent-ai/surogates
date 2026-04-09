export interface User {
  id: string;
  username: string;
  name: string;
  email: string | null;
  avatar_url: string | null;
  last_login_at: string | null;
  created_at: string;
}