#include "mdp/protocol.hpp"
#include <iostream>

int main() {
    std::ios::sync_with_stdio(false);
    std::cin.tie(nullptr);
    mdp::Book book;
    std::string line;
    while (std::getline(std::cin, line)) {
        if (line == "QUIT") break;
        if (line == "STATUS" || line == "DUMP") {
            mdp::write_status(std::cout, book, "status", line == "DUMP");
        } else {
            try {
                const auto command = mdp::parse(line);
                const auto status = book.apply(command);
                mdp::write_status(std::cout, book, status);
            } catch (const std::exception&) {
                book.invalidate("malformed_command");
                mdp::write_status(std::cout, book, "invalid");
            }
        }
        std::cout.flush();
    }
    return std::cin.bad() ? 1 : 0;
}
