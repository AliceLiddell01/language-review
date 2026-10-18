/**
 * Не удалось открыть файл: {path}.
 * @brief Проверьте настройки.
 */
void run(const char* path, Logger* logger) {
    printf("Не удалось прочитать %s: %d", path, 7);
    printf(R"tag(Не удалось открыть %s)tag", path);
    logger->error("Не удалось открыть файл.");
    auto protocol = "ACK";
}
