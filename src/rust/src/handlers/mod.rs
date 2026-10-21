//! ActivityPub handlers — mirrors `pubby.handlers`.

pub mod client;
pub mod discovery;
pub mod handler;
pub mod inbox;
pub mod multi;
pub mod outbox;

pub use discovery::{NodeInfoStats, NodeInfoStatsFn};
pub use handler::{ActivityPubHandler, HandlerOptions};
pub use inbox::{
    ActivityDisposition, FollowPolicyFn, InboxProcessor, OnActivityReceived, OnFollowResponse,
    OnInteractionReceived,
};
pub use multi::{ActorResolver, MultiActorHandler};
pub use outbox::{DeliverFn, DeliveryOutcome, OnDeliveryResult, OutboxProcessor};
