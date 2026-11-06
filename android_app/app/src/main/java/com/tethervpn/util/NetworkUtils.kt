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

    data class NetworkIp(val interfaceName: String, val ipAddress: String, val isHotspot: Boolean)

    fun getLocalIpAddresses(): List<NetworkIp> {
        val list = mutableListOf<NetworkIp>()
        try {
            val interfaces = Collections.list(NetworkInterface.getNetworkInterfaces())
            for (intf in interfaces) {
                if (intf.isLoopback || !intf.isUp) continue

                val addrs = Collections.list(intf.inetAddresses)
                for (addr in addrs) {
                    if (addr is Inet4Address && !addr.isLoopbackAddress) {
                        val host = addr.hostAddress ?: continue
                        val name = intf.name.lowercase()
                        val isHotspot = name.contains("ap") || 
                                        name.contains("swlan") || 
                                        name.contains("rndis") ||
                                        host.startsWith("192.168.43.") ||
                                        host.startsWith("192.168.44.")

                        list.add(NetworkIp(intf.displayName ?: intf.name, host, isHotspot))
                    }
                }
            }
        } catch (ignored: Exception) {}

        return list.sortedByDescending { it.isHotspot }
    }

    fun getPrimaryIp(): String {
        val addresses = getLocalIpAddresses()
        val hotspot = addresses.firstOrNull { it.isHotspot }
        if (hotspot != null) return hotspot.ipAddress

        val anyLocal = addresses.firstOrNull { !it.ipAddress.startsWith("10.") && !it.ipAddress.startsWith("127.") }
        if (anyLocal != null) return anyLocal.ipAddress

        return addresses.firstOrNull()?.ipAddress ?: "192.168.43.1"
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
