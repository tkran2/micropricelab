#pragma once
#include "book.hpp"
#include <charconv>
#include <iomanip>
#include <ostream>
#include <sstream>
#include <stdexcept>

namespace mdp {
template <class T> T number(const std::string& token) {
    T result{};
    const auto parsed = std::from_chars(token.data(), token.data() + token.size(), result);
    if (parsed.ec != std::errc{} || parsed.ptr != token.data() + token.size())
        throw std::invalid_argument("invalid_integer");
    return result;
}
inline Command parse(const std::string& line) {
    if (line.size() > 16 * 1024 * 1024) throw std::invalid_argument("command_too_large");
    std::istringstream in(line);
    std::string type, token;
    if (!(in >> type >> token)) throw std::invalid_argument("missing_generation");
    Command c{Kind::reset, number<std::uint64_t>(token), 0, {}, {}};
    if (type == "RESET") c.kind = Kind::reset;
    else if (type == "INVALIDATE") {
        c.kind = Kind::invalidate;
        if (!(in >> c.reason) || c.reason.empty() || c.reason.size() > 64 ||
            c.reason.find_first_not_of("abcdefghijklmnopqrstuvwxyz0123456789_") != std::string::npos)
            throw std::invalid_argument("invalid_reason");
    } else if (type == "TICK") {
        c.kind = Kind::tick;
        if (!(in >> token)) throw std::invalid_argument("missing_sequence");
        c.sequence = number<std::uint64_t>(token);
    } else if (type == "SNAP" || type == "UPD") {
        c.kind = type == "SNAP" ? Kind::snapshot : Kind::update;
        if (!(in >> token)) throw std::invalid_argument("missing_sequence");
        c.sequence = number<std::uint64_t>(token);
        if (!(in >> token)) throw std::invalid_argument("missing_count");
        const auto count = number<std::size_t>(token);
        if (count > 250000) throw std::invalid_argument("too_many_updates");
        c.levels.reserve(count);
        for (std::size_t i = 0; i < count; ++i) {
            std::string side, price, qty;
            if (!(in >> side >> price >> qty) || side.size() != 1)
                throw std::invalid_argument("missing_level");
            c.levels.push_back({side[0], number<Value>(price), number<Value>(qty)});
        }
    } else throw std::invalid_argument("unknown_command");
    if (in >> token) throw std::invalid_argument("trailing_token");
    return c;
}
inline void write_status(std::ostream& out, const Book& book, std::string_view status,
                         bool dump = false) {
    out << "{\"status\":\"" << status << "\",\"trusted\":" << (book.trusted() ? "true" : "false")
        << ",\"generation\":" << book.generation() << ",\"sequence\":";
    if (book.sequence()) out << *book.sequence(); else out << "null";
    out << ",\"reason\":\"" << book.reason() << "\",\"bid_levels\":" << book.bids().size()
        << ",\"ask_levels\":" << book.asks().size()
        << ",\"gaps\":" << book.gaps() << ",\"duplicates\":" << book.duplicates()
        << ",\"snapshots\":" << book.snapshots() << ",\"digest\":\""
        << std::hex << std::setw(16) << std::setfill('0') << book.digest() << std::dec
        << "\",\"bid\":";
    if (book.trusted() && !book.bids().empty())
        out << '[' << book.bids().rbegin()->first << ',' << book.bids().rbegin()->second << ']';
    else out << "null";
    out << ",\"ask\":";
    if (book.trusted() && !book.asks().empty())
        out << '[' << book.asks().begin()->first << ',' << book.asks().begin()->second << ']';
    else out << "null";
    if (dump) {
        for (const char side : {'B', 'A'}) {
            out << (side == 'B' ? ",\"bids\":[" : ",\"asks\":[");
            bool first = true;
            const auto& levels = side == 'B' ? book.bids() : book.asks();
            for (const auto& [price, qty] : levels) {
                if (!first) out << ',';
                first = false;
                out << '[' << price << ',' << qty << ']';
            }
            out << ']';
        }
    }
    out << "}\n";
}
} // namespace mdp
