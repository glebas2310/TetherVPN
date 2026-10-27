package com.everyproxy.fluent.proxy

import android.util.Log
import java.io.InputStream
import java.io.OutputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketException
import java.net.SocketTimeoutException
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

class HttpProxyServer(
    val port: Int = 8080,
    private val logCallback: ((String, String) -> Unit)? = null
) {
    companion object {
        private const val TAG = "HttpProxyServer"
        private const val BUFFER_SIZE = 16384
        private const val SOCKET_TIMEOUT_MS = 30000
    }

    private var serverSocket: ServerSocket? = null
    private var threadPool: ExecutorService? = null
    private val isRunning = AtomicBoolean(false)
    val stats = ProxyStats()

    fun isRunning(): Boolean = isRunning.get()

    fun start() {
        if (isRunning.getAndSet(true)) return

        stats.reset()
        threadPool = Executors.newCachedThreadPool { runnable ->
            Thread(runnable).apply {
                isDaemon = true
                name = "ProxyThread-${System.currentTimeMillis() % 1000}"
            }
        }

        threadPool?.execute {
            try {
                // Слушаем все интерфейсы (0.0.0.0) для приема подключений по Wi-Fi Hotspot и USB
                serverSocket = ServerSocket(port, 256, InetAddress.getByName("0.0.0.0")).apply {
                    reuseAddress = true
                }
                log("OK", "HTTP Прокси сервер запущен на 0.0.0.0:$port")

                while (isRunning.get()) {
                    try {
                        val clientSocket = serverSocket?.accept() ?: break
                        clientSocket.tcpNoDelay = true
                        clientSocket.soTimeout = SOCKET_TIMEOUT_MS

                        stats.totalConnections.incrementAndGet()
                        stats.activeConnections.incrementAndGet()

                        threadPool?.execute {
                            handleClient(clientSocket)
                        }
                    } catch (e: SocketException) {
                        if (!isRunning.get()) break
                        Log.w(TAG, "SocketException: ${e.message}")
                    } catch (e: Exception) {
                        Log.e(TAG, "Ошибка при accept соединения: ${e.message}", e)
                    }
                }
            } catch (e: Exception) {
                log("ERROR", "Критическая ошибка запуска прокси: ${e.message}")
            } finally {
                stop()
            }
        }
    }

    fun stop() {
        if (!isRunning.getAndSet(false)) return
        try {
            serverSocket?.close()
            serverSocket = null
        } catch (e: Exception) {
            Log.e(TAG, "Ошибка закрытия ServerSocket", e)
        }
        try {
            threadPool?.shutdownNow()
            threadPool = null
        } catch (e: Exception) {
            Log.e(TAG, "Ошибка остановки threadPool", e)
        }
        log("INFO", "Прокси сервер остановлен.")
    }

    private fun handleClient(clientSocket: Socket) {
        val clientIp = clientSocket.inetAddress?.hostAddress ?: "unknown"
        try {
            val clientIn = clientSocket.getInputStream()
            val clientOut = clientSocket.getOutputStream()

            val requestLine = readLine(clientIn) ?: run {
                safeClose(clientSocket)
                stats.activeConnections.decrementAndGet()
                return
            }

            val parts = requestLine.trim().split(" ")
            if (parts.size < 2) {
                safeClose(clientSocket)
                stats.activeConnections.decrementAndGet()
                return
            }

            val method = parts[0].uppercase()
            val target = parts[1]

            if (method == "CONNECT") {
                // HTTPS туннелирование
                handleConnect(clientSocket, clientIn, clientOut, target, clientIp)
            } else {
                // Обычный HTTP запрос (GET, POST и т.д.)
                handleHttp(clientSocket, clientIn, clientOut, requestLine, clientIp)
            }
        } catch (e: Exception) {
            // Обычные обрывы соединений браузером
        } finally {
            safeClose(clientSocket)
            stats.activeConnections.decrementAndGet()
        }
    }

    private fun handleConnect(
        clientSocket: Socket,
        clientIn: InputStream,
        clientOut: OutputStream,
        target: String,
        clientIp: String
    ) {
        // Пропускаем оставшиеся заголовки запроса CONNECT до пустой строки \r\n
        while (true) {
            val line = readLine(clientIn) ?: break
            if (line.isEmpty() || line == "\r") break
        }

        val hostAndPort = target.split(":")
        val remoteHost = hostAndPort[0]
        val remotePort = if (hostAndPort.size > 1) hostAndPort[1].toIntOrNull() ?: 443 else 443

        var remoteSocket: Socket? = null
        try {
            // Android маршрутизирует этот сокет через текущий активный мобильный VPN!
            remoteSocket = Socket(remoteHost, remotePort).apply {
                tcpNoDelay = true
                soTimeout = SOCKET_TIMEOUT_MS
            }

            // Отвечаем клиенту: 200 Connection Established
            val response = "HTTP/1.1 200 Connection Established\r\nProxy-Agent: EveryProxyFluent\r\n\r\n"
            clientOut.write(response.toByteArray(Charsets.US_ASCII))
            clientOut.flush()

            val remoteIn = remoteSocket.getInputStream()
            val remoteOut = remoteSocket.getOutputStream()

            // Двунаправленная потоковая передача байтов
            val t1 = Thread {
                pipeStreams(clientIn, remoteOut, isUpload = true)
                safeClose(remoteSocket)
                safeClose(clientSocket)
            }
            val t2 = Thread {
                pipeStreams(remoteIn, clientOut, isUpload = false)
                safeClose(clientSocket)
                safeClose(remoteSocket)
            }

            t1.isDaemon = true
            t2.isDaemon = true
            t1.start()
            t2.start()

            t1.join()
            t2.join()
        } catch (e: Exception) {
            safeClose(remoteSocket)
        }
    }

    private fun handleHttp(
        clientSocket: Socket,
        clientIn: InputStream,
        clientOut: OutputStream,
        firstLine: String,
        clientIp: String
    ) {
        val headers = mutableListOf<String>()
        var host = ""
        var port = 80

        while (true) {
            val line = readLine(clientIn) ?: break
            if (line.isEmpty() || line == "\r") break
            headers.add(line)
            if (line.startsWith("Host:", ignoreCase = true)) {
                val hostVal = line.substring(5).trim()
                if (hostVal.contains(":")) {
                    val hp = hostVal.split(":")
                    host = hp[0]
                    port = hp[1].toIntOrNull() ?: 80
                } else {
                    host = hostVal
                }
            }
        }

        if (host.isEmpty()) return

        var remoteSocket: Socket? = null
        try {
            remoteSocket = Socket(host, port).apply {
                tcpNoDelay = true
                soTimeout = SOCKET_TIMEOUT_MS
            }

            val remoteOut = remoteSocket.getOutputStream()
            val remoteIn = remoteSocket.getInputStream()

            // Переписываем первую строку запроса из абсолютного URI в относительный
            val parts = firstLine.split(" ")
            val relUri = if (parts.size >= 2 && parts[1].startsWith("http://", ignoreCase = true)) {
                val uri = parts[1].substring(7)
                val slashIdx = uri.indexOf('/')
                if (slashIdx != -1) uri.substring(slashIdx) else "/"
            } else {
                parts.getOrElse(1) { "/" }
            }

            val rewrittenFirstLine = "${parts[0]} $relUri ${parts.getOrElse(2) { "HTTP/1.1" }}\r\n"
            remoteOut.write(rewrittenFirstLine.toByteArray(Charsets.US_ASCII))

            for (h in headers) {
                // Исключаем Proxy-Connection заголовок
                if (!h.startsWith("Proxy-Connection:", ignoreCase = true)) {
                    remoteOut.write("$h\r\n".toByteArray(Charsets.US_ASCII))
                }
            }
            remoteOut.write("\r\n".toByteArray(Charsets.US_ASCII))
            remoteOut.flush()

            val t1 = Thread {
                pipeStreams(clientIn, remoteOut, isUpload = true)
                safeClose(remoteSocket)
            }
            val t2 = Thread {
                pipeStreams(remoteIn, clientOut, isUpload = false)
                safeClose(clientSocket)
            }

            t1.isDaemon = true
            t2.isDaemon = true
            t1.start()
            t2.start()

            t1.join()
            t2.join()
        } catch (e: Exception) {
            safeClose(remoteSocket)
        }
    }

    private fun pipeStreams(input: InputStream, output: OutputStream, isUpload: Boolean) {
        val buffer = ByteArray(BUFFER_SIZE)
        try {
            var bytesRead: Int
            while (isRunning.get()) {
                bytesRead = input.read(buffer)
                if (bytesRead == -1) break
                output.write(buffer, 0, bytesRead)
                output.flush()

                if (isUpload) {
                    stats.bytesReceived.addAndGet(bytesRead.toLong())
                } else {
                    stats.bytesSent.addAndGet(bytesRead.toLong())
                }
            }
        } catch (e: Exception) {
            // Нормальное завершение потока при закрытии сокета
        }
    }

    private fun readLine(input: InputStream): String? {
        val sb = StringBuilder()
        var c: Int
        while (true) {
            c = input.read()
            if (c == -1) {
                return if (sb.isEmpty()) null else sb.toString()
            }
            if (c == '\n'.code) break
            if (c != '\r'.code) {
                sb.append(c.toChar())
            }
        }
        return sb.toString()
    }

    private fun safeClose(socket: Socket?) {
        try {
            socket?.close()
        } catch (_: Exception) {}
    }

    private fun log(level: String, msg: String) {
        Log.i(TAG, "[$level] $msg")
        logCallback?.invoke(level, msg)
    }
}
