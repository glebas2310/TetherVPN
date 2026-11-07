package com.tethervpn.client

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.net.HttpURLConnection
import java.net.InetSocketAddress
import java.net.Proxy
import java.net.URL

object ProxyChecker {

    sealed class CheckResult {
        data class Success(val latencyMs: Long) : CheckResult()
        data class Error(val message: String) : CheckResult()
    }

    suspend fun checkProxy(host: String, port: Int): CheckResult = withContext(Dispatchers.IO) {
        val proxy = Proxy(Proxy.Type.HTTP, InetSocketAddress(host, port))
        val testUrl = URL("http://cp.cloudflare.com/generate_204")
        var connection: HttpURLConnection? = null

        val startTime = System.currentTimeMillis()
        try {
            connection = testUrl.openConnection(proxy) as HttpURLConnection
            connection.connectTimeout = 3500
            connection.readTimeout = 3500
            connection.instanceFollowRedirects = false
            connection.useCaches = false
            connection.setRequestProperty("User-Agent", "TetherVPN/1.0")

            val responseCode = connection.responseCode
            val latency = System.currentTimeMillis() - startTime

            if (responseCode in 200..399) {
                CheckResult.Success(latency)
            } else {
                CheckResult.Error("HTTP статус $responseCode")
            }
        } catch (e: Exception) {
            val msg = e.localizedMessage ?: e.javaClass.simpleName
            CheckResult.Error(msg)
        } finally {
            connection?.disconnect()
        }
    }
}
