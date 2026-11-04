package com.tethervpn.proxy

import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import java.util.Locale

object ProxyStats {
    val activeConnections = AtomicInteger(0)
    val bytesReceived = AtomicLong(0)
    val bytesSent = AtomicLong(0)

    fun reset() {
        activeConnections.set(0)
        bytesReceived.set(0)
        bytesSent.set(0)
    }

    fun formatBytes(bytes: Long): String {
        if (bytes < 1024) return "$bytes B"
        val kb = bytes / 1024.0
        if (kb < 1024) return String.format(Locale.US, "%.1f KB", kb)
        val mb = kb / 1024.0
        if (mb < 1024) return String.format(Locale.US, "%.2f MB", mb)
        val gb = mb / 1024.0
        return String.format(Locale.US, "%.2f GB", gb)
    }
}
