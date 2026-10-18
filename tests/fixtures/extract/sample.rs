//! Внутренняя документация: {reader.TokenType}.
/// Не удалось открыть {}: {path}.
fn run(path: &str) {
    println!("Не удалось прочитать {}: {}", path, path);
    let message = format!("Не удалось открыть {}: {path}", path);
    tracing!("Повторная попытка чтения для {path}");
    println!(r#"Повторите попытку для {reader.TokenType}."#);
    let machine_status = "FAILED";
    let protocol_key = "user.name";
    let _ = (message, machine_status, protocol_key);
}
