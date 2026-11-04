package com.tethervpn.proxy

import android.util.Log
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketTimeoutException
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

class HttpProxyServer(
    private val port: Int,
    private val onStateChange: ((Boolean, String?) -> Unit)? = null
) {
    private val tag = "TetherVPN:HttpProxy"
    private var serverSocket: ServerSocket? = null
    private val isRunning = AtomicBoolean(false)
    private val executor = Executors.newCachedThreadPool { runnable ->
        Thread(runnable).apply { isDaemon = true }
    }

    fun start() {
        if (isRunning.get()) return

        try {
            val server = ServerSocket()
            server.reuseAddress = true
            server.bind(InetSocketAddress("0.0.0.0", port), 256)
            serverSocket = server
            isRunning.set(true)
            ProxyStats.reset()
            onStateChange?.invoke(true, null)
            Log.i(tag, "HTTP Proxy listening on 0.0.0.0:$port")

            executor.execute {
                while (isRunning.get() && !server.isClosed) {
                    try {
                        val clientSocket = server.accept()
                        clientSocket.tcpNoDelay = true
                        clientSocket.keepAlive = true
                        clientSocket.soTimeout = 30000

                        executor.execute {
                            handleClient(clientSocket)
                        }
                    } catch (e: Exception) {
                        if (!isRunning.get() || server.isClosed) break
                        Log.e(tag, "Accept error: ${e.message}")
                    }
                }
            }
        } catch (e: Exception) {
            isRunning.set(false)
            onStateChange?.invoke(false, e.message)
            Log.e(tag, "Failed to start proxy on port $port: ${e.message}", e)
            throw e
        }
    }

    fun stop() {
        if (!isRunning.compareAndSet(true, false)) return

        try {
            serverSocket?.close()
        } catch (ignored: Exception) {}
        serverSocket = null

        executor.shutdown()
        try {
            if (!executor.awaitTermination(1, TimeUnit.SECONDS)) {
                executor.shutdownNow()
            }
        } catch (e: InterruptedException) {
            executor.shutdownNow()
        }

        onStateChange?.invoke(false, null)
        Log.i(tag, "HTTP Proxy stopped")
    }

    private fun handleClient(clientSocket: Socket) {
        ProxyStats.activeConnections.incrementAndGet()
        var remoteSocket: Socket? = null

        try {
            val clientIn = BufferedInputStream(clientSocket.getInputStream())
            val clientOut = BufferedOutputStream(clientSocket.getOutputStream())

            val requestLine = readLine(clientIn) ?: return
            if (requestLine.isBlank()) return

            val parts = requestLine.trim().split(" ")
            if (parts.size < 2) return

            val method = parts[0].uppercase()
            val target = parts[1]

            if (method == "CONNECT") {
                // HTTPS Tunneling
                handleConnect(target, clientSocket, clientIn, clientOut)
            } else {
                // Plain HTTP forwarding
                handleHttp(requestLine, clientSocket, clientIn, clientOut)
            }
        } catch (ignored: SocketTimeoutException) {
            // Normal connection inactivity
        } catch (e: Exception) {
            // Broken pipe or reset by peer is normal in proxy workloads
        } finally {
            ProxyStats.activeConnections.decrementAndGet()
            closeQuietly(clientSocket)
        }
    }

    private fun handleConnect(
        target: String,
        clientSocket: Socket,
        clientIn: InputStream,
        clientOut: OutputStream
    ) {
        val host: String
        val port: Int

        if (target.contains(":")) {
            val idx = target.lastIndexOf(':')
            host = target.substring(0, idx)
            port = target.substring(idx + 1).toIntOrNull() ?: 443
        } else {
            host = target
            port = 443
        }

        // Consume and discard remaining client HTTP headers
        while (true) {
            val line = readLine(clientIn) ?: break
            if (line.isEmpty() || line == "\r\n" || line == "\n") break
        }

        // Establish connection to destination (routed via Android active VPN)
        val remoteSocket = Socket()
        remoteSocket.tcpNoDelay = true
        remoteSocket.keepAlive = true
        remoteSocket.soTimeout = 30000
        remoteSocket.connect(InetSocketAddress(host, port), 10000)

        // Send 200 Connection Established to client
        val response = "HTTP/1.1 200 Connection Established\r\nProxy-Agent: TetherVPN/1.0\r\n\r\n"
        clientOut.write(response.toByteArray(Charsets.US_ASCII))
        clientOut.flush()

        val remoteIn = BufferedInputStream(remoteSocket.getInputStream())
        val remoteOut = BufferedOutputStream(remoteSocket.getOutputStream())

        // Pipe bidirectional traffic
        val pipe1 = executor.submit {
            pipeStream(clientIn, remoteOut, isClientToRemote = true)
        }
        val pipe2 = executor.submit {
            pipeStream(remoteIn, clientOut, isClientToRemote = false)
        }

        try {
            pipe1.get()
        } catch (ignored: Exception) {}
        try {
            pipe2.get()
        } catch (ignored: Exception) {}

        closeQuietly(remoteSocket)
    }

    private fun handleHttp(
        initialRequestLine: String,
        clientSocket: Socket,
        clientIn: InputStream,
        clientOut: OutputStream
    ) {
        val headers = mutableListOf<String>()
        var host = ""
        var port = 80

        while (true) {
            val line = readLine(clientIn) ?: break
            if (line.isEmpty() || line == "\r\n" || line == "\n") break
            headers.add(line)

            if (line.startsWith("Host:", ignoreCase = true)) {
                val hostHeader = line.substring(5).trim()
                if (hostHeader.contains(":")) {
                    val idx = hostHeader.lastIndexOf(':')
                    host = hostHeader.substring(0, idx)
                    port = hostHeader.substring(idx + 1).toIntOrNull() ?: 80
                } else {
                    host = hostHeader
                    port = 80
                }
            }
        }

        if (host.isEmpty()) return

        // Rewrite request line: strip absolute URL
        val parts = initialRequestLine.trim().split(" ")
        val method = parts[0]
        var uri = parts[1]
        val protocol = if (parts.size > 2) parts[2] else "HTTP/1.1"

        if (uri.startsWith("http://", ignoreCase = true)) {
            val slashIdx = uri.indexOf('/', 7)
            uri = if (slashIdx != -1) uri.substring(slashIdx) else "/"
        }

        val rewrittenRequestLine = "$method $uri $protocol\r\n"

        // Connect to remote host (routed via Android active VPN)
        val remoteSocket = Socket()
        remoteSocket.tcpNoDelay = true
        remoteSocket.keepAlive = true
        remoteSocket.soTimeout = 30000
        remoteSocket.connect(InetSocketAddress(host, port), 10000)

        val remoteIn = BufferedInputStream(remoteSocket.getInputStream())
        val remoteOut = BufferedOutputStream(remoteSocket.getOutputStream())

        // Forward rewritten request line and headers
        remoteOut.write(rewrittenRequestLine.toByteArray(Charsets.US_ASCII))
        for (h in headers) {
            remoteOut.write((h + "\r\n").toByteArray(Charsets.US_ASCII))
        }
        remoteOut.write("\r\n".toByteArray(Charsets.US_ASCII))
        remoteOut.flush()

        // Bidirectional streaming
        val pipe1 = executor.submit {
            pipeStream(clientIn, remoteOut, isClientToRemote = true)
        }
        val pipe2 = executor.submit {
            pipeStream(remoteIn, clientOut, isClientToRemote = false)
        }

        try {
            pipe1.get()
        } catch (ignored: Exception) {}
        try {
            pipe2.get()
        } catch (ignored: Exception) {}

        closeQuietly(remoteSocket)
    }

    private fun pipeStream(input: InputStream, output: OutputStream, isClientToRemote: Boolean) {
        val buffer = ByteArray(16384)
        try {
            while (isRunning.get()) {
                val bytesRead = input.read(buffer)
                if (bytesRead == -1) break

                output.write(buffer, 0, bytesRead)
                output.flush()

                if (isClientToRemote) {
                    ProxyStats.bytesSent.addAndGet(bytesRead.toLong())
                } else {
                    ProxyStats.bytesReceived.addAndGet(bytesRead.toLong())
                }
            }
        } catch (ignored: Exception) {
        } finally {
            try { output.flush() } catch (ignored: Exception) {}
        }
    }

    private fun readLine(input: InputStream): String? {
        val baos = ByteArrayOutputStream()
        var prev = -1
        while (true) {
            val b = input.read()
            if (b == -1) {
                if (baos.size() == 0) return null
                break
            }
            if (b == '\n'.code) {
                if (prev == '\r'.code && baos.size() > 0) {
                    // Strip the trailing \r if present
                    val bytes = baos.toByteArray()
                    return String(bytes, 0, bytes.size - 1, Charsets.ISO_8859_1)
                }
                break
            }
            baos.write(b)
            prev = b
        }
        return baos.toString(Charsets.ISO_8859_1.name())
    }

    private fun closeQuietly(socket: Socket?) {
        try {
            socket?.close()
        } catch (ignored: Exception) {}
    }
}
