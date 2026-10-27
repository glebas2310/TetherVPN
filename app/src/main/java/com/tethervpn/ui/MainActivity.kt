package com.tethervpn.ui

import android.Manifest
import android.content.BroadcastReceiver
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.view.View
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.tethervpn.R
import com.tethervpn.databinding.ActivityMainBinding
import com.tethervpn.service.ProxyForegroundService
import com.tethervpn.util.NetworkUtils
import com.tethervpn.util.OxygenOptimizer
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private var statsJob: Job? = null

    private val statusReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            updateUiState()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        checkPermissions()
        setupListeners()
        updateIpAddress()
        updateUiState()
        startStatsPolling()

        if (OxygenOptimizer.isBatteryOptimizationIgnored(this)) {
            binding.tvOnePlusProtected.visibility = View.VISIBLE
        }
    }

    override fun onResume() {
        super.onResume()
        val filter = IntentFilter(ProxyForegroundService.ACTION_STATUS_BROADCAST)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(statusReceiver, filter, Context.RECEIVER_EXPORTED)
        } else {
            registerReceiver(statusReceiver, filter)
        }
        updateIpAddress()
        updateUiState()
    }

    override fun onPause() {
        super.onPause()
        try {
            unregisterReceiver(statusReceiver)
        } catch (_: Exception) {}
    }

    override fun onDestroy() {
        statsJob?.cancel()
        super.onDestroy()
    }

    private fun checkPermissions() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED
            ) {
                ActivityCompat.requestPermissions(
                    this,
                    arrayOf(Manifest.permission.POST_NOTIFICATIONS),
                    101
                )
            }
        }
    }

    private fun setupListeners() {
        binding.btnToggleProxy.setOnClickListener {
            if (ProxyForegroundService.isServiceRunning) {
                stopProxyService()
            } else {
                startProxyService()
            }
        }

        binding.btnCopyIp.setOnClickListener {
            val ip = binding.tvIpAddress.text.toString()
            val clipboard = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
            clipboard.setPrimaryClip(ClipData.newPlainText("TetherVPN IP", ip))
            Toast.makeText(this, "IP адрес скопирован: $ip", Toast.LENGTH_SHORT).show()
        }

        binding.btnCheckVpn.setOnClickListener {
            runVpnHealthCheck()
        }

        binding.btnOnePlusGuide.setOnClickListener {
            showOnePlusGuideDialog()
        }
    }

    private fun updateIpAddress() {
        val ip = NetworkUtils.getLocalIpAddress()
        binding.tvIpAddress.text = ip
    }

    private fun startProxyService() {
        val portStr = binding.etPort.text.toString().trim()
        val port = portStr.toIntOrNull() ?: 8080

        if (port !in 1..65535) {
            Toast.makeText(this, "Некорректный порт: $port (разрешено 1-65535)", Toast.LENGTH_SHORT).show()
            return
        }

        val intent = Intent(this, ProxyForegroundService::class.java).apply {
            action = ProxyForegroundService.ACTION_START
            putExtra(ProxyForegroundService.EXTRA_PORT, port)
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent)
        } else {
            startService(intent)
        }

        if (!OxygenOptimizer.isBatteryOptimizationIgnored(this)) {
            OxygenOptimizer.requestIgnoreBatteryOptimization(this)
        }

        binding.root.postDelayed({ updateUiState() }, 200)
    }

    private fun stopProxyService() {
        val intent = Intent(this, ProxyForegroundService::class.java).apply {
            action = ProxyForegroundService.ACTION_STOP
        }
        startService(intent)
        binding.root.postDelayed({ updateUiState() }, 200)
    }

    private fun updateUiState() {
        val isRunning = ProxyForegroundService.isServiceRunning
        if (isRunning) {
            binding.tvStatusBadge.text = getString(R.string.status_active)
            binding.tvStatusBadge.setBackgroundResource(R.drawable.bg_chip_active)

            binding.btnToggleProxy.text = getString(R.string.btn_stop)
            binding.btnToggleProxy.setBackgroundResource(R.drawable.bg_button_danger)

            binding.etPort.isEnabled = false
            binding.tvIpAddress.text = ProxyForegroundService.activeIp
        } else {
            binding.tvStatusBadge.text = getString(R.string.status_stopped)
            binding.tvStatusBadge.setBackgroundResource(R.drawable.bg_chip_inactive)

            binding.btnToggleProxy.text = getString(R.string.btn_start)
            binding.btnToggleProxy.setBackgroundResource(R.drawable.bg_button_primary)

            binding.etPort.isEnabled = true
        }
    }

    private fun runVpnHealthCheck() {
        binding.btnCheckVpn.isEnabled = false
        binding.tvVpnDiagResult.visibility = View.VISIBLE
        binding.tvVpnDiagResult.text = "⏳ Проверка сквозного интернет-соединения через VPN..."

        lifecycleScope.launch {
            val diag = NetworkUtils.checkVpnHealth()
            binding.btnCheckVpn.isEnabled = true
            if (diag.isWorking) {
                binding.tvVpnDiagResult.setTextColor(ContextCompat.getColor(this@MainActivity, R.color.status_success))
                val ext = if (diag.externalIp != null) " (Внешний IP: ${diag.externalIp})" else ""
                val lat = if (diag.latencyMs != null) " [${diag.latencyMs} мс]" else ""
                binding.tvVpnDiagResult.text = "✅ VPN РАБОТАЕТ! ${diag.location ?: "Интернет доступен"}$ext$lat"
            } else {
                binding.tvVpnDiagResult.setTextColor(ContextCompat.getColor(this@MainActivity, R.color.status_danger))
                binding.tvVpnDiagResult.text = "⚠️ ВНИМАНИЕ: ${diag.message}\nПроверьте, включен ли VPN на смартфоне."
            }
        }
    }

    private fun showOnePlusGuideDialog() {
        val steps = OxygenOptimizer.ONEPLUS_SETUP_STEPS.joinToString("\n\n")

        AlertDialog.Builder(this)
            .setTitle("🛡️ Защита от закрытия в фоне (OnePlus 9 Pro)")
            .setMessage(
                "Чтобы OxygenOS не выгружала TetherVPN в спящем режиме, выполните следующие шаги:\n\n$steps"
            )
            .setPositiveButton("Исключить из оптимизации батареи") { _, _ ->
                OxygenOptimizer.requestIgnoreBatteryOptimization(this)
            }
            .setNeutralButton("Настройки приложения") { _, _ ->
                OxygenOptimizer.openAppDetailsSettings(this)
            }
            .setNegativeButton("Закрыть", null)
            .show()
    }

    private fun startStatsPolling() {
        statsJob = lifecycleScope.launch {
            while (isActive) {
                if (ProxyForegroundService.isServiceRunning) {
                    val stats = ProxyForegroundService.serverInstance?.stats
                    if (stats != null) {
                        binding.tvBytesReceived.text = stats.formatBytes(stats.bytesReceived.get())
                        binding.tvBytesSent.text = stats.formatBytes(stats.bytesSent.get())
                        binding.tvActiveConnections.text =
                            "Активных TCP соединений: ${stats.activeConnections.get()} (Всего: ${stats.totalConnections.get()})"
                    }
                }
                delay(1500)
            }
        }
    }
}
