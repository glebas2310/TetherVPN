package com.everyproxy.fluent.proxy

import java.util.concurrent.atomic.AtomicLong

data class ProxyStats(
    val activeConnections: AtomicLong = AtomicLong(0),
    val totalConnections: AtomicLong = AtomicLong(0),
    val bytesReceived: AtomicLong = AtomicLong(0),
    val bytesSent: AtomicLong = AtomicLong(0)
) {
    fun reset() {
        activeConnections.set(0)
        totalConnections.set(0)
        bytesReceived.set(0)
        bytesSent.set(0)
    }

    fun formatBytes(bytes: Long): String {
        return when {
            bytes < 1024 -> "$bytes B"
            bytes < 1024 * 1024 -> String.format("%.1f KB", bytes / 1024.0)
            bytes < 1024 * 1024 * 1024 -> String.format("%.1f MB", bytes / (1024.0 * 1024.0))
            else -> String.format("%.2f GB", bytes / (1024.0 * 1024.0 * 1024.0))
        }
    }
}
