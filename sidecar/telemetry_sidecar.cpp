#ifdef _WIN32
#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0600
#endif
#endif

#include <chrono>
#include <cmath>
#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#pragma comment(lib, "Ws2_32.lib")
#else
#include <arpa/inet.h>
#include <netdb.h>
#include <sys/socket.h>
#include <sys/statvfs.h>
#include <unistd.h>
#endif

namespace {

constexpr const char* kDefaultRedisHost = "127.0.0.1";
constexpr int kDefaultRedisPort = 6379;
constexpr const char* kDefaultRedisKey = "icarus:metrics:sidecar";
constexpr int kDefaultIntervalMs = 2000;
constexpr const char* kDefaultSource = "local-sidecar";

struct Sample {
    double cpu_percent;
    double memory_used_mb;
    double memory_total_mb;
    double disk_used_percent;
    bool has_gpu_util_percent;
    double gpu_util_percent;
};

struct CpuSnapshot {
#ifdef _WIN32
    unsigned long long idle = 0;
    unsigned long long kernel = 0;
    unsigned long long user = 0;
#else
    unsigned long long idle = 0;
    unsigned long long total = 0;
#endif
};

#ifdef _WIN32
unsigned long long FileTimeToU64(const FILETIME& ft) {
    ULARGE_INTEGER li;
    li.LowPart = ft.dwLowDateTime;
    li.HighPart = ft.dwHighDateTime;
    return li.QuadPart;
}
#endif

std::string NowIsoUtc() {
    using namespace std::chrono;
    auto now = system_clock::now();
    auto t = system_clock::to_time_t(now);

    std::tm tm_utc{};
#ifdef _WIN32
#if defined(_MSC_VER)
    gmtime_s(&tm_utc, &t);
#else
    std::tm* p = std::gmtime(&t);
    if (p) {
        tm_utc = *p;
    }
#endif
#else
    gmtime_r(&t, &tm_utc);
#endif

    auto ms = duration_cast<milliseconds>(now.time_since_epoch()) % 1000;

    std::ostringstream out;
    out << std::put_time(&tm_utc, "%Y-%m-%dT%H:%M:%S")
        << '.' << std::setw(3) << std::setfill('0') << ms.count() << 'Z';
    return out.str();
}

bool ReadGpuUtilPercent(double* out_value) {
#ifdef _WIN32
    const char* gpu_cmd = "cmd /C \"where nvidia-smi >nul 2>nul && nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>nul\"";
#if defined(_MSC_VER)
    FILE* pipe = _popen(gpu_cmd, "r");
#else
    FILE* pipe = popen(gpu_cmd, "r");
#endif
#else
    FILE* pipe = popen("nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null", "r");
#endif
    if (!pipe) {
        return false;
    }

    char buffer[128] = {0};
    if (!fgets(buffer, sizeof(buffer), pipe)) {
#if defined(_WIN32) && defined(_MSC_VER)
        _pclose(pipe);
#else
        pclose(pipe);
#endif
        return false;
    }

#if defined(_WIN32) && defined(_MSC_VER)
    _pclose(pipe);
#else
    pclose(pipe);
#endif

    try {
        double value = std::stod(buffer);
        if (value < 0.0 || value > 100.0) {
            return false;
        }
        *out_value = value;
        return true;
    } catch (...) {
        return false;
    }
}

CpuSnapshot ReadCpuSnapshot() {
    CpuSnapshot snap;
#ifdef _WIN32
    FILETIME idle, kernel, user;
    if (GetSystemTimes(&idle, &kernel, &user)) {
        snap.idle = FileTimeToU64(idle);
        snap.kernel = FileTimeToU64(kernel);
        snap.user = FileTimeToU64(user);
    }
#else
    FILE* f = fopen("/proc/stat", "r");
    if (!f) {
        return snap;
    }

    char cpu_label[8] = {0};
    unsigned long long user = 0, nice = 0, system = 0, idle = 0;
    unsigned long long iowait = 0, irq = 0, softirq = 0, steal = 0;
    if (fscanf(f, "%7s %llu %llu %llu %llu %llu %llu %llu %llu",
               cpu_label, &user, &nice, &system, &idle,
               &iowait, &irq, &softirq, &steal) == 9) {
        snap.idle = idle + iowait;
        snap.total = user + nice + system + idle + iowait + irq + softirq + steal;
    }
    fclose(f);
#endif
    return snap;
}

double CpuPercentFromSnapshots(const CpuSnapshot& a, const CpuSnapshot& b) {
#ifdef _WIN32
    const auto idle_delta = static_cast<double>(b.idle - a.idle);
    const auto kernel_delta = static_cast<double>(b.kernel - a.kernel);
    const auto user_delta = static_cast<double>(b.user - a.user);
    const auto total_delta = kernel_delta + user_delta;
#else
    const auto idle_delta = static_cast<double>(b.idle - a.idle);
    const auto total_delta = static_cast<double>(b.total - a.total);
#endif
    if (total_delta <= 0.0) {
        return 0.0;
    }

    const double usage = (1.0 - (idle_delta / total_delta)) * 100.0;
    return std::max(0.0, std::min(100.0, usage));
}

std::pair<double, double> ReadMemoryMb() {
#ifdef _WIN32
    MEMORYSTATUSEX mem{};
    mem.dwLength = sizeof(mem);
    if (!GlobalMemoryStatusEx(&mem)) {
        return {0.0, 1.0};
    }
    const double total_mb = static_cast<double>(mem.ullTotalPhys) / (1024.0 * 1024.0);
    const double avail_mb = static_cast<double>(mem.ullAvailPhys) / (1024.0 * 1024.0);
    return {total_mb - avail_mb, total_mb};
#else
    FILE* f = fopen("/proc/meminfo", "r");
    if (!f) {
        return {0.0, 1.0};
    }

    unsigned long long total_kb = 0;
    unsigned long long avail_kb = 0;
    char key[64] = {0};
    unsigned long long value = 0;
    char unit[16] = {0};

    while (fscanf(f, "%63s %llu %15s", key, &value, unit) == 3) {
        if (std::strcmp(key, "MemTotal:") == 0) {
            total_kb = value;
        } else if (std::strcmp(key, "MemAvailable:") == 0) {
            avail_kb = value;
        }
        if (total_kb > 0 && avail_kb > 0) {
            break;
        }
    }
    fclose(f);

    if (total_kb == 0) {
        return {0.0, 1.0};
    }

    const double total_mb = static_cast<double>(total_kb) / 1024.0;
    const double avail_mb = static_cast<double>(avail_kb) / 1024.0;
    return {total_mb - avail_mb, total_mb};
#endif
}

double ReadDiskUsedPercent() {
#ifdef _WIN32
    ULARGE_INTEGER free_bytes_available{};
    ULARGE_INTEGER total_bytes{};
    ULARGE_INTEGER total_free_bytes{};
    if (!GetDiskFreeSpaceExA("C:\\", &free_bytes_available, &total_bytes, &total_free_bytes)) {
        return 0.0;
    }
    const double capacity = static_cast<double>(total_bytes.QuadPart);
    const double free = static_cast<double>(total_free_bytes.QuadPart);
#else
    struct statvfs fs{};
    if (statvfs("/", &fs) != 0) {
        return 0.0;
    }
    const double capacity = static_cast<double>(fs.f_blocks) * static_cast<double>(fs.f_frsize);
    const double free = static_cast<double>(fs.f_bfree) * static_cast<double>(fs.f_frsize);
#endif

    if (capacity <= 0.0) {
        return 0.0;
    }

    const double used_pct = ((capacity - free) / capacity) * 100.0;
    return std::max(0.0, std::min(100.0, used_pct));
}

std::string EscapeJson(const std::string& input) {
    std::string out;
    out.reserve(input.size() + 16);
    for (char c : input) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default: out += c; break;
        }
    }
    return out;
}

std::string ToJson(const Sample& s, const std::string& source) {
    std::ostringstream out;
    out.setf(std::ios::fixed);
    out.precision(2);

    out << "{"
        << "\"timestamp\":\"" << NowIsoUtc() << "\"," 
        << "\"source\":\"" << EscapeJson(source) << "\"," 
        << "\"metrics\":{"
        << "\"cpu_percent\":" << s.cpu_percent << ","
        << "\"memory_used_mb\":" << s.memory_used_mb << ","
        << "\"memory_total_mb\":" << s.memory_total_mb << ","
        << "\"disk_used_percent\":" << s.disk_used_percent;

    if (s.has_gpu_util_percent) {
        out << ",\"gpu_util_percent\":" << s.gpu_util_percent;
    }

    out << "}}";
    return out.str();
}

class RedisPublisher {
  public:
    RedisPublisher(std::string host, int port)
        : host_(std::move(host)), port_(port), sock_(-1) {}

    ~RedisPublisher() {
        Close();
#ifdef _WIN32
        if (wsa_ready_) {
            WSACleanup();
        }
#endif
    }

    bool Connect() {
#ifdef _WIN32
        if (!wsa_ready_) {
            WSADATA wsa{};
            if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
                std::cerr << "WSAStartup failed\n";
                return false;
            }
            wsa_ready_ = true;
        }
#endif
        Close();

        struct addrinfo hints{};
        hints.ai_family = AF_UNSPEC;
        hints.ai_socktype = SOCK_STREAM;

        struct addrinfo* result = nullptr;
        const std::string port_s = std::to_string(port_);
        if (getaddrinfo(host_.c_str(), port_s.c_str(), &hints, &result) != 0 || !result) {
            return false;
        }

        bool connected = false;
        for (auto* p = result; p != nullptr; p = p->ai_next) {
            int sock = static_cast<int>(socket(p->ai_family, p->ai_socktype, p->ai_protocol));
            if (sock < 0) {
                continue;
            }
            if (connect(sock, p->ai_addr, static_cast<int>(p->ai_addrlen)) == 0) {
                sock_ = sock;
                connected = true;
                break;
            }
#ifdef _WIN32
            closesocket(sock);
#else
            close(sock);
#endif
        }

        freeaddrinfo(result);
        return connected;
    }

    bool LPush(const std::string& key, const std::string& value) {
        if (sock_ < 0 && !Connect()) {
            return false;
        }

        std::ostringstream req;
        req << "*3\r\n"
            << "$5\r\nLPUSH\r\n"
            << "$" << key.size() << "\r\n" << key << "\r\n"
            << "$" << value.size() << "\r\n" << value << "\r\n";
        const std::string wire = req.str();

        if (!SendAll(wire)) {
            Close();
            return false;
        }

        std::string reply;
        if (!ReadLine(reply)) {
            Close();
            return false;
        }

        if (reply.empty()) {
            Close();
            return false;
        }

        const char t = reply[0];
        if (t == '+' || t == ':') {
            return true;
        }

        std::cerr << "Redis error reply: " << reply << "\n";
        return false;
    }

    void Close() {
        if (sock_ >= 0) {
#ifdef _WIN32
            closesocket(sock_);
#else
            close(sock_);
#endif
            sock_ = -1;
        }
    }

  private:
    bool SendAll(const std::string& msg) {
        size_t sent_total = 0;
        while (sent_total < msg.size()) {
#ifdef _WIN32
            int sent = send(sock_, msg.data() + sent_total, static_cast<int>(msg.size() - sent_total), 0);
#else
            ssize_t sent = send(sock_, msg.data() + sent_total, msg.size() - sent_total, 0);
#endif
            if (sent <= 0) {
                return false;
            }
            sent_total += static_cast<size_t>(sent);
        }
        return true;
    }

    bool ReadLine(std::string& out) {
        out.clear();
        char c = 0;
        while (true) {
#ifdef _WIN32
            int rc = recv(sock_, &c, 1, 0);
#else
            ssize_t rc = recv(sock_, &c, 1, 0);
#endif
            if (rc <= 0) {
                return false;
            }
            if (c == '\r') {
#ifdef _WIN32
                int rc2 = recv(sock_, &c, 1, 0);
#else
                ssize_t rc2 = recv(sock_, &c, 1, 0);
#endif
                if (rc2 <= 0) {
                    return false;
                }
                if (c == '\n') {
                    return true;
                }
                out.push_back('\r');
                out.push_back(c);
            } else {
                out.push_back(c);
            }
            if (out.size() > 4096) {
                return false;
            }
        }
    }

    std::string host_;
    int port_;
    int sock_;
#ifdef _WIN32
    bool wsa_ready_ = false;
#endif
};

Sample CollectSample(const CpuSnapshot& prev, const CpuSnapshot& current, bool enable_gpu_probe) {
    std::pair<double, double> mem_pair = ReadMemoryMb();
    double mem_used = mem_pair.first;
    double mem_total = mem_pair.second;
    Sample s{};
    s.cpu_percent = CpuPercentFromSnapshots(prev, current);
    s.memory_used_mb = mem_used;
    s.memory_total_mb = mem_total;
    s.disk_used_percent = ReadDiskUsedPercent();
    s.has_gpu_util_percent = false;
    if (enable_gpu_probe) {
        s.has_gpu_util_percent = ReadGpuUtilPercent(&s.gpu_util_percent);
    }
    return s;
}

int EnvInt(const char* key, int fallback) {
    const char* raw = std::getenv(key);
    if (!raw || !*raw) {
        return fallback;
    }
    try {
        return std::stoi(raw);
    } catch (...) {
        return fallback;
    }
}

std::string EnvString(const char* key, const char* fallback) {
    const char* raw = std::getenv(key);
    if (!raw || !*raw) {
        return std::string(fallback);
    }
    return std::string(raw);
}

void SleepMs(int milliseconds) {
    if (milliseconds <= 0) {
        return;
    }
#ifdef _WIN32
    Sleep(static_cast<DWORD>(milliseconds));
#else
    usleep(static_cast<useconds_t>(milliseconds * 1000));
#endif
}

}  // namespace

int main() {
    const std::string redis_host = EnvString("ICARUS_REDIS_HOST", kDefaultRedisHost);
    const int redis_port = EnvInt("ICARUS_REDIS_PORT", kDefaultRedisPort);
    const std::string redis_key = EnvString("ICARUS_REDIS_KEY", kDefaultRedisKey);
    const int interval_ms = std::max(500, EnvInt("ICARUS_INTERVAL_MS", kDefaultIntervalMs));
    const std::string source = EnvString("ICARUS_SOURCE", kDefaultSource);
#ifdef _WIN32
    const bool enable_gpu_probe = EnvInt("ICARUS_ENABLE_GPU", 0) == 1;
#else
    const bool enable_gpu_probe = EnvInt("ICARUS_ENABLE_GPU", 1) == 1;
#endif

    RedisPublisher publisher(redis_host, redis_port);

    CpuSnapshot prev = ReadCpuSnapshot();
    SleepMs(200);

    std::cerr << "[sidecar] publishing to redis " << redis_host << ':' << redis_port
              << " key=" << redis_key << " interval_ms=" << interval_ms << "\n";

    while (true) {
        const auto loop_start = std::chrono::steady_clock::now();

        CpuSnapshot current = ReadCpuSnapshot();
        Sample sample = CollectSample(prev, current, enable_gpu_probe);
        prev = current;

        const std::string payload = ToJson(sample, source);
        bool ok = publisher.LPush(redis_key, payload);

        std::cout << payload << std::endl;
        if (!ok) {
            std::cerr << "[sidecar] publish failed, reconnecting next loop\n";
        }

        const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now() - loop_start);
        const int sleep_for = std::max(0, interval_ms - static_cast<int>(elapsed.count()));
        SleepMs(sleep_for);
    }

    return 0;
}
