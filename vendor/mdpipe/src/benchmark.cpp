#include "mdp/book.hpp"
#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <random>
#include <stdexcept>

using Clock = std::chrono::steady_clock;
int main(int argc, char** argv) {
    try {
        std::size_t events = 100000, trials = 5;
        for (int i = 1; i < argc; i += 2) {
            if (i + 1 == argc) throw std::invalid_argument("missing argument");
            const std::string flag = argv[i];
            const auto n = std::stoull(argv[i + 1]);
            if (flag == "--events") events = static_cast<std::size_t>(n);
            else if (flag == "--trials") trials = static_cast<std::size_t>(n);
            else throw std::invalid_argument("unknown argument");
        }
        if (events < 1 || events > 10000000 || trials < 1 || trials > 100)
            throw std::invalid_argument("argument outside benchmark limits");
        std::cout << "profile,trial,events,levels,events_per_second,p50_ns,p99_ns,max_ns,digest\n";
        for (const auto depth : {100, 10000}) {
            std::mt19937_64 rng(17);
            mdp::Command snapshot{mdp::Kind::snapshot, 1, 0, {}, {}};
            for (int i = 0; i < depth; ++i) {
                snapshot.levels.push_back({'B', (100000 - i) * 100000000LL, 100000000});
                snapshot.levels.push_back({'A', (100002 + i) * 100000000LL, 100000000});
            }
            std::vector<mdp::Command> trace;
            trace.reserve(events);
            for (std::size_t i = 0; i < events; ++i) {
                const bool bid = (rng() & 1U) != 0;
                const auto offset = static_cast<std::int64_t>(rng() % static_cast<std::uint64_t>(depth));
                trace.push_back({mdp::Kind::update, 1, static_cast<std::uint64_t>(i + 1), {},
                    {{bid ? 'B' : 'A', (bid ? 100000 - offset : 100002 + offset) * 100000000LL,
                      static_cast<std::int64_t>(rng() % 100 + 1) * 1000000LL}}});
            }
            const auto initialise = [&](mdp::Book& book) {
                book.apply({mdp::Kind::reset, 1, 0, {}, {}});
                book.apply(snapshot);
            };
            for (std::size_t trial = 1; trial <= trials; ++trial) {
                mdp::Book book;
                initialise(book);
                const auto start = Clock::now();
                for (const auto& event : trace) book.apply(event);
                const double seconds = std::chrono::duration<double>(Clock::now() - start).count();
                const auto digest = book.digest();
                mdp::Book timed;
                initialise(timed);
                std::vector<std::int64_t> latencies;
                latencies.reserve(events);
                for (const auto& event : trace) {
                    const auto before = Clock::now();
                    timed.apply(event);
                    latencies.push_back(std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now() - before).count());
                }
                if (timed.digest() != digest || !book.trusted()) throw std::runtime_error("benchmark state mismatch");
                std::sort(latencies.begin(), latencies.end());
                const auto quantile = [&](std::size_t percent) { return latencies[(events * percent + 99) / 100 - 1]; };
                std::cout << (depth == 100 ? "shallow" : "deep") << ',' << trial << ',' << events << ','
                          << depth * 2 << ',' << std::fixed << std::setprecision(2)
                          << static_cast<double>(events) / seconds << ',' << quantile(50) << ',' << quantile(99)
                          << ',' << latencies.back() << ',' << std::hex << std::setw(16) << std::setfill('0')
                          << digest << std::dec << '\n';
            }
        }
    } catch (const std::exception& e) {
        std::cerr << e.what() << '\n';
        return 1;
    }
}
