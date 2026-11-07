package com.tethervpn.util

import android.content.Context
import android.net.ConnectivityManager
import android.net.wifi.WifiManager
import android.os.Build
import java.net.Inet4Address
import java.net.NetworkInterface
import java.util.Collections
import java.util.Locale

object NetworkUtils {

    const val TYPE_WIFI = "Точка доступа Wi-Fi"
    const val TYPE_USB = "USB-модем"
    const val TYPE_LAN = "Локальная сеть"

    data class TetherInfo(
        val ipAddress: String,
        val connectionType: String,
        val interfaceName: String = ""
    ) {
        val typeName: String get() = connectionType
        val isHotspot: Boolean
            get() = connectionType == TYPE_WIFI || connectionType == TYPE_USB
    }

    data class NetworkIp(
        val interfaceName: String,
        val ipAddress: String,
        val isHotspot: Boolean,
        val connectionType: String = TYPE_LAN
    )

    /**
     * Determines connection type and score based on interface name and IPv4 address.
     * Higher score indicates higher priority for proxy tethering binding.
     */
    private fun classifyInterface(name: String, host: String): Pair<String, Int> {
        val lower = name.lowercase()
        return when {
            // USB Tethering interfaces: rndis0, usb0, ncm0, or typical subnet 192.168.42.x
            lower.contains("rndis") || lower.contains("usb") || lower.contains("ncm") || host.startsWith("192.168.42.") -> {
                Pair(TYPE_USB, 100)
            }
            // Dedicated Wi-Fi Hotspot interfaces/subnets: ap0, swlan0, 192.168.43.x, 192.168.44.x
            lower.contains("ap") || lower.contains("swlan") || host.startsWith("192.168.43.") || host.startsWith("192.168.44.") -> {
                Pair(TYPE_WIFI, 90)
            }
            // General Wi-Fi interface (wlan0, etc.)
            lower.contains("wlan") -> {
                Pair(TYPE_WIFI, 50)
            }
            // Other local networks (e.g. eth0, local subnet)
            !host.startsWith("10.") && !host.startsWith("127.") && !host.startsWith("169.254.") -> {
                Pair(TYPE_LAN, 20)
            }
            else -> {
                Pair(TYPE_LAN, 10)
            }
        }
    }

    /**
     * Retrieves all available non-loopback IPv4 network addresses with structured tether info.
     */
    fun getTetherInfoList(): List<TetherInfo> {
        val list = mutableListOf<Pair<TetherInfo, Int>>()
        try {
            val interfaces = Collections.list(NetworkInterface.getNetworkInterfaces())
            for (intf in interfaces) {
                if (intf.isLoopback || !intf.isUp) continue

                val addrs = Collections.list(intf.inetAddresses)
                for (addr in addrs) {
                    if (addr is Inet4Address && !addr.isLoopbackAddress) {
                        val host = addr.hostAddress ?: continue
                        val (connType, score) = classifyInterface(intf.name, host)
                        val info = TetherInfo(
                            ipAddress = host,
                            connectionType = connType,
                            interfaceName = intf.displayName ?: intf.name
                        )
                        list.add(Pair(info, score))
                    }
                }
            }
        } catch (ignored: Exception) {}

        return list.sortedByDescending { it.second }.map { it.first }
    }

    /**
     * Backward-compatible helper for retrieving local IP addresses.
     */
    fun getLocalIpAddresses(): List<NetworkIp> {
        return getTetherInfoList().map {
            NetworkIp(
                interfaceName = it.interfaceName,
                ipAddress = it.ipAddress,
                isHotspot = it.isHotspot,
                connectionType = it.connectionType
            )
        }
    }

    /**
     * Returns structured info for primary active tether interface (USB-модем or Точка доступа Wi-Fi).
     */
    fun getPrimaryTetherInfo(): TetherInfo {
        val list = getTetherInfoList()
        return list.firstOrNull() ?: TetherInfo(
            ipAddress = "192.168.43.1",
            connectionType = TYPE_WIFI,
            interfaceName = "wlan0"
        )
    }

    /**
     * Backward-compatible alias for primary tether info.
     */
    fun getTetherInfo(): TetherInfo = getPrimaryTetherInfo()

    /**
     * Returns the primary IP address to display or bind to.
     */
    fun getPrimaryIp(): String {
        return getPrimaryTetherInfo().ipAddress
    }

    /**
     * Determines default Wi-Fi gateway IP (e.g. 192.168.43.1) of the connected hotspot.
     */
    fun getWifiGatewayIp(context: Context): String {
        try {
            val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                val activeNetwork = cm?.activeNetwork
                if (activeNetwork != null) {
                    val linkProps = cm.getLinkProperties(activeNetwork)
                    if (linkProps != null) {
                        for (route in linkProps.routes) {
                            if (route.isDefaultRoute && route.gateway is Inet4Address) {
                                val gw = route.gateway?.hostAddress
                                if (!gw.isNullOrEmpty() && gw != "0.0.0.0") {
                                    return gw
                                }
                            }
                        }
                    }
                }
            }

            @Suppress("DEPRECATION")
            val wm = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as? WifiManager
            @Suppress("DEPRECATION")
            val dhcp = wm?.dhcpInfo
            if (dhcp != null && dhcp.gateway != 0) {
                val g = dhcp.gateway
                return String.format(
                    Locale.US,
                    "%d.%d.%d.%d",
                    g and 0xff,
                    g shr 8 and 0xff,
                    g shr 16 and 0xff,
                    g shr 24 and 0xff
                )
            }
        } catch (ignored: Exception) {}

        return "192.168.43.1"
    }
}
