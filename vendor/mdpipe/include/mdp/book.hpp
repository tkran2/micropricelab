#pragma once

#include <cstdint>
#include <limits>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <string_view>
#include <vector>

namespace mdp {
using Value = std::int64_t;
struct Level { char side; Value price; Value quantity; };
enum class Kind { reset, invalidate, snapshot, update, tick };
struct Command {
    Kind kind;
    std::uint64_t generation = 0;
    std::uint64_t sequence = 0;
    std::string reason;
    std::vector<Level> levels;
};

inline void hash_byte(std::uint64_t& h, std::uint8_t v) {
    h ^= v;
    h *= 1099511628211ULL;
}
inline void hash_word(std::uint64_t& h, std::uint64_t v) {
    for (unsigned i = 0; i < 8; ++i) {
        hash_byte(h, static_cast<std::uint8_t>(v & 255U));
        v >>= 8U;
    }
}
inline std::uint64_t level_hash(const Level& level) {
    std::uint64_t h = 14695981039346656037ULL;
    hash_byte(h, static_cast<std::uint8_t>(level.side));
    hash_word(h, static_cast<std::uint64_t>(level.price));
    hash_word(h, static_cast<std::uint64_t>(level.quantity));
    return h;
}

// One writer owns the complete book. Quotes are available only in trusted state.
// Generations fence delayed frames from a closed/replaced WebSocket connection.
class Book {
public:
    explicit Book(std::size_t max_levels = 250000) : max_levels_(max_levels) {}
    std::uint64_t generation() const { return generation_; }
    std::optional<std::uint64_t> sequence() const { return sequence_; }
    bool trusted() const { return trusted_; }
    const std::string& reason() const { return reason_; }
    const auto& bids() const { return bids_; }
    const auto& asks() const { return asks_; }
    std::uint64_t gaps() const { return gaps_; }
    std::uint64_t duplicates() const { return duplicates_; }
    std::uint64_t snapshots() const { return snapshots_; }

    void invalidate(std::string reason) {
        trusted_ = false;
        waiting_ = false;
        reason_ = std::move(reason);
        sequence_.reset();
        bids_.clear();
        asks_.clear();
        levels_hash_ = 0;
    }

    std::string_view apply(const Command& c) {
        if (c.kind == Kind::reset) {
            if (c.generation <= generation_) return "stale_generation";
            invalidate("awaiting_snapshot");
            generation_ = c.generation;
            waiting_ = true;
            return "reset";
        }
        if (c.generation != generation_) return "stale_generation";
        if (c.kind == Kind::invalidate) {
            invalidate(c.reason);
            return "invalidated";
        }
        if (c.kind == Kind::snapshot) {
            if (!waiting_) {
                invalidate("unexpected_snapshot");
                return "invalid";
            }
        } else if (c.kind == Kind::update) {
            if (!trusted_) return "untrusted";
        } else if (!waiting_ && !trusted_) return "untrusted";
        // The exchange's envelope sequence includes subscriptions and heartbeats.
        // Check continuity before changing the book, including before a snapshot.
        if (sequence_) {
            if (c.sequence <= *sequence_) {
                ++duplicates_;
                return "duplicate";
            }
            if (*sequence_ == std::numeric_limits<std::uint64_t>::max() ||
                c.sequence != *sequence_ + 1) {
                ++gaps_;
                invalidate("sequence_gap");
                return "gap";
            }
        }
        if (c.kind == Kind::tick) {
            sequence_ = c.sequence;
            return "tick";
        }
        // Validate the whole packet before changing any level.
        std::set<std::pair<char, Value>> snapshot_keys;
        for (const auto& level : c.levels) {
            if ((level.side != 'B' && level.side != 'A') ||
                level.price <= 0 || level.quantity < 0) {
                invalidate("invalid_level");
                return "invalid";
            }
            if (c.kind == Kind::snapshot && !snapshot_keys.emplace(level.side, level.price).second) {
                invalidate("duplicate_snapshot_level");
                return "invalid";
            }
        }
        if (c.kind == Kind::snapshot) {
            bids_.clear();
            asks_.clear();
            levels_hash_ = 0;
        }
        for (const auto& level : c.levels) {
            auto& side = level.side == 'B' ? bids_ : asks_;
            auto it = side.find(level.price);
            if (it != side.end()) {
                if (c.kind == Kind::snapshot) {
                    invalidate("duplicate_snapshot_level");
                    return "invalid";
                }
                levels_hash_ ^= level_hash({level.side, level.price, it->second});
            }
            if (level.quantity == 0) {
                if (it != side.end()) side.erase(it);
            } else {
                side[level.price] = level.quantity;
                levels_hash_ ^= level_hash(level);
            }
            if (bids_.size() + asks_.size() > max_levels_) {
                invalidate("level_limit");
                return "invalid";
            }
        }
        if (!bids_.empty() && !asks_.empty() && bids_.rbegin()->first >= asks_.begin()->first) {
            invalidate("crossed_book");
            return "invalid";
        }
        sequence_ = c.sequence;
        trusted_ = true;
        waiting_ = false;
        reason_ = "ok";
        if (c.kind == Kind::snapshot) ++snapshots_;
        return c.kind == Kind::snapshot ? "snapshot" : "applied";
    }

    // O(1) reproducible fingerprint. SHA-256 journal hashing handles integrity;
    // this is a non-cryptographic, incrementally updated book checksum.
    std::uint64_t digest() const {
        std::uint64_t h = 14695981039346656037ULL;
        hash_word(h, generation_);
        hash_word(h, sequence_.value_or(0));
        hash_byte(h, sequence_.has_value() ? 1 : 0);
        hash_byte(h, trusted_ ? 1 : 0);
        hash_word(h, static_cast<std::uint64_t>(bids_.size()));
        hash_word(h, static_cast<std::uint64_t>(asks_.size()));
        hash_word(h, levels_hash_);
        return h;
    }

private:
    std::size_t max_levels_;
    std::uint64_t generation_ = 0;
    std::optional<std::uint64_t> sequence_;
    bool waiting_ = false;
    bool trusted_ = false;
    std::string reason_ = "cold_start";
    std::map<Value, Value> bids_, asks_;
    std::uint64_t levels_hash_ = 0;
    std::uint64_t gaps_ = 0, duplicates_ = 0, snapshots_ = 0;
};
} // namespace mdp
