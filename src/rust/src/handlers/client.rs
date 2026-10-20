//! Default `User-Agent` helper — mirrors `pubby.handlers._client`.

/// Get the default User-Agent header for outgoing HTTP requests.
///
/// :param actor_id: This server's actor ID (URL).
pub fn get_default_user_agent(actor_id: &str) -> String {
    format!("pubby/{} (+{actor_id})", crate::VERSION)
}
