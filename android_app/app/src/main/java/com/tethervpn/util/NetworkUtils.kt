package com.tethervpn.util

import java.net.Inet4Address
import java.net.NetworkInterface
import java.util.Collections

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

        // Sort: Hotspot first, then Wi-Fi, then others
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
}
