package com.tethervpn.ui

import android.Manifest
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.graphics.Color
import android.os.Build
import android.os.Bundle
import android.view.View
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.tethervpn.R
import com.tethervpn.databinding.ActivityMainBinding
import com.tethervpn.proxy.ProxyStats
import com.tethervpn.service.ProxyForegroundService
import com.tethervpn.util.NetworkUtils
import com.tethervpn.util.SamsungOptimizer
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private val prefs by lazy { getSharedPreferences("tethervpn_prefs", Context.MODE_PRIVATE) }

    private val requestNotificationPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { _ -> }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        checkPermissions()
        setupViews()
        observeServiceState()
        startStatsUpdateLoop()
    }

    override fun onResume() {
        super.onResume()
        updateIpAddress()
        updateBatteryOptimizationState()
    }

    private fun checkPermissions() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED
            ) {
                requestNotificationPermissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
            }
        }
    }

    private fun setupViews() {
        // Load saved port
        val savedPort = prefs.getInt("proxy_port", 8080)
        binding.etPort.setText(savedPort.toString())

        // Refresh IP
        binding.btnRefreshIp.setOnClickListener {
            updateIpAddress()
            Toast.makeText(this, "IP refreshed", Toast.LENGTH_SHORT).show()
        }

        // Copy IP:Port
        binding.btnCopyAddress.setOnClickListener {
            val ip = binding.tvIpAddress.text.toString()
            val port = binding.etPort.text.toString().trim()
            val formatted = "$ip:$port"

            val clipboard = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
            val clip = ClipData.newPlainText("TetherVPN Proxy", formatted)
            clipboard.setPrimaryClip(clip)

            Toast.makeText(this, getString(R.string.copied_toast) + " ($formatted)", Toast.LENGTH_SHORT).show()
        }

        // Toggle Proxy
        binding.btnToggleProxy.setOnClickListener {
            if (ProxyForegroundService.isRunningFlow.value) {
                ProxyForegroundService.stopService(this)
            } else {
                val portStr = binding.etPort.text.toString().trim()
                val port = portStr.toIntOrNull() ?: 8080
                if (port in 1024..65535) {
                    prefs.edit().putInt("proxy_port", port).apply()
                    ProxyForegroundService.startService(this, port)
                } else {
                    Toast.makeText(this, "Enter valid port between 1024 and 65535", Toast.LENGTH_LONG).show()
                }
            }
        }

        // Samsung Battery Optimization
        binding.btnBatteryExempt.setOnClickListener {
            SamsungOptimizer.requestIgnoreBatteryOptimization(this)
        }
    }

    private fun updateIpAddress() {
        val ip = NetworkUtils.getPrimaryIp()
        binding.tvIpAddress.text = ip
    }

    private fun updateBatteryOptimizationState() {
        val isExempt = SamsungOptimizer.isIgnoringBatteryOptimizations(this)
        if (isExempt) {
            binding.btnBatteryExempt.text = getString(R.string.samsung_exempt_done)
            binding.btnBatteryExempt.setTextColor(ContextCompat.getColor(this, R.color.accent_green))
            binding.btnBatteryExempt.isEnabled = false
        } else {
            binding.btnBatteryExempt.text = getString(R.string.samsung_btn_exempt)
            binding.btnBatteryExempt.setTextColor(ContextCompat.getColor(this, R.color.accent_blue))
            binding.btnBatteryExempt.isEnabled = true
        }
    }

    private fun observeServiceState() {
        lifecycleScope.launch {
            ProxyForegroundService.isRunningFlow.collect { running ->
                updateUiForRunningState(running)
            }
        }
    }

    private fun updateUiForRunningState(running: Boolean) {
        if (running) {
            binding.tvStatusBadge.text = getString(R.string.status_running)
            binding.tvStatusBadge.setTextColor(ContextCompat.getColor(this, R.color.accent_green))
            binding.tvStatusBadge.backgroundTintList = ColorStateList.valueOf(
                ContextCompat.getColor(this, R.color.accent_green_bg)
            )

            binding.btnToggleProxy.text = getString(R.string.btn_stop)
            binding.btnToggleProxy.backgroundTintList = ColorStateList.valueOf(
                ContextCompat.getColor(this, R.color.accent_red)
            )
            binding.tvToggleHint.text = "HTTP Proxy active • Routing traffic through phone"

            binding.etPort.isEnabled = false
            binding.cardStats.visibility = View.VISIBLE
        } else {
            binding.tvStatusBadge.text = getString(R.string.status_stopped)
            binding.tvStatusBadge.setTextColor(ContextCompat.getColor(this, R.color.accent_red))
            binding.tvStatusBadge.backgroundTintList = ColorStateList.valueOf(
                ContextCompat.getColor(this, R.color.accent_red_bg)
            )

            binding.btnToggleProxy.text = getString(R.string.btn_start)
            binding.btnToggleProxy.backgroundTintList = ColorStateList.valueOf(
                ContextCompat.getColor(this, R.color.accent_blue)
            )
            binding.tvToggleHint.text = "Tap to start HTTP proxy"

            binding.etPort.isEnabled = true
            binding.cardStats.visibility = View.GONE
        }
    }

    private fun startStatsUpdateLoop() {
        lifecycleScope.launch {
            while (isActive) {
                if (ProxyForegroundService.isRunningFlow.value) {
                    val activeConn = ProxyStats.activeConnections.get()
                    val sent = ProxyStats.formatBytes(ProxyStats.bytesSent.get())
                    val received = ProxyStats.formatBytes(ProxyStats.bytesReceived.get())

                    binding.tvActiveConnections.text = getString(R.string.stats_active_connections, activeConn)
                    binding.tvTrafficStats.text = getString(R.string.stats_traffic, sent, received)
                }
                delay(1000)
            }
        }
    }
}
