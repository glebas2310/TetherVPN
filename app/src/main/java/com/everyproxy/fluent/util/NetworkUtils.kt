package com.everyproxy.fluent.util

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.Inet4Address
import java.net.NetworkInterface
import java.net.URL

data class VpnDiagResult(
    val isWorking: Boolean,
    val externalIp: String? = null,
    val location: String? = null,
    val latencyMs: Long? = null,
    val message: String
)

object NetworkUtils {

    /**
     * Определяет локальный IPv4 адрес смартфона (адрес точки доступа 192.168.43.1 или Wi-Fi).
     */
    fun getLocalIpAddress(): String {
        try {
            val interfaces = NetworkInterface.getNetworkInterfaces()
            var fallbackIp = "192.168.43.1"
            while (interfaces.hasMoreElements()) {
                val iface = interfaces.nextElement()
                if (iface.isLoopback || !iface.isUp) continue

                val name = iface.name.lowercase()
                val isHotspot = name.contains("wlan") || name.contains("ap") || name.contains("rndis")

                val addresses = iface.inetAddresses
                while (addresses.hasMoreElements()) {
                    val addr = addresses.nextElement()
                    if (!addr.isLoopbackAddress && addr is Inet4Address) {
                        val host = addr.hostAddress ?: continue
                        if (host.startsWith("192.168.43.") || host.startsWith("172.20.10.")) {
                            return host
                        }
                        if (isHotspot) {
                            fallbackIp = host
                        }
                    }
                }
            }
            return fallbackIp
        } catch (_: Exception) {
            return "192.168.43.1"
        }
    }

    /**
     * Сквозная проверка интернет-трафика через активный VPN смартфона.
     */
    suspend fun checkVpnHealth(): VpnDiagResult = withContext(Dispatchers.IO) {
        val t0 = System.currentTimeMillis()
        var ok = false

        // 1. Проверяем доступность 204 Check
        val testUrls = listOf(
            "http://connectivitycheck.gstatic.com/generate_204",
            "http://cp.cloudflare.com/generate_204"
        )

        for (urlStr in testUrls) {
            try {
                val url = URL(urlStr)
                val conn = (url.openConnection() as HttpURLConnection).apply {
                    connectTimeout = 2500
                    readTimeout = 2500
                    instanceFollowRedirects = false
                }
                val code = conn.responseCode
                conn.disconnect()
                if (code in 200..204) {
                    ok = true
                    break
                }
            } catch (_: Exception) {
                continue
            }
        }

        if (!ok) {
            return@withContext VpnDiagResult(
                isWorking = false,
                message = "VPN или интернет на смартфоне недоступен!"
            )
        }

        val latency = System.currentTimeMillis() - t0

        // 2. Получаем внешний IP и геолокацию
        var extIp: String? = null
        var location: String? = null

        try {
            val ipUrl = URL("http://icanhazip.com")
            val conn = (ipUrl.openConnection() as HttpURLConnection).apply {
                connectTimeout = 2000
                readTimeout = 2000
            }
            val reader = BufferedReader(InputStreamReader(conn.inputStream))
            extIp = reader.readLine()?.trim()
            reader.close()
            conn.disconnect()

            if (!extIp.isNullOrEmpty()) {
                val geoUrl = URL("http://ip-api.com/json/$extIp?lang=ru")
                val geoConn = (geoUrl.openConnection() as HttpURLConnection).apply {
                    connectTimeout = 2000
                    readTimeout = 2000
                }
                val geoReader = BufferedReader(InputStreamReader(geoConn.inputStream))
                val jsonStr = geoReader.readText()
                geoReader.close()
                geoConn.disconnect()

                val json = JSONObject(jsonStr)
                if (json.optString("status") == "success") {
                    val country = json.optString("country")
                    val city = json.optString("city")
                    location = if (city.isNotEmpty()) "$country, $city" else country
                }
            }
        } catch (_: Exception) {}

        return@withContext VpnDiagResult(
            isWorking = true,
            externalIp = extIp,
            location = location,
            latencyMs = latency,
            message = "VPN активен! ${location ?: ""} ($latency мс)"
        )
    }
}
