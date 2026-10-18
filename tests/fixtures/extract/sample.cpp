/**
 * Не удалось открыть файл: {path}.
 * @brief Проверьте настройки.
 */
void run(const char* path) {
    printf("Не удалось прочитать %s: %d", path, 7);
    printf(R"tag(Не удалось открыть %s)tag", path);
    auto protocol = "ACK";
}
