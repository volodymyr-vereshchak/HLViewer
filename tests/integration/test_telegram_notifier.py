import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta
import pandas as pd

from backend.telegram_notifier.telegram_norifier import TelegramBot
from backend.telegram_notifier.email_notifier import EmailNotifier
from backend.hl_engine.hostlib_updater import HostlibUpdater
from backend.db.models import Line, HourlyArchive
from backend.db.dao.telegram_user_dao import TelegramUserDao


class TestTelegramBot:
    """Test Telegram bot functionality."""
    
    @pytest.fixture
    def mock_bot(self):
        """Create mock Telegram bot."""
        with patch('backend.telegram_notifier.telegram_norifier.Bot') as mock_bot_class:
            mock_bot = MagicMock()
            mock_bot_class.return_value = mock_bot
            mock_bot.send_message = AsyncMock()
            mock_bot.session.close = AsyncMock()
            mock_bot.close = AsyncMock()
            yield mock_bot
    
    @pytest.fixture
    def mock_dispatcher(self):
        """Create mock dispatcher."""
        with patch('backend.telegram_notifier.telegram_norifier.Dispatcher') as mock_dp_class:
            mock_dp = MagicMock()
            mock_dp_class.return_value = mock_dp
            mock_dp.message.register = MagicMock()
            mock_dp.start_polling = AsyncMock()
            mock_dp.stop_polling = AsyncMock()
            yield mock_dp
    
    @pytest.fixture
    def telegram_bot(self, mock_bot, mock_dispatcher):
        """Create TelegramBot instance with mocked dependencies."""
        with patch('backend.telegram_notifier.telegram_norifier.backend_settings') as mock_settings:
            mock_settings.get.return_value = "test_token"
            bot = TelegramBot()
            return bot
    
    def test_telegram_bot_initialization(self, mock_bot, mock_dispatcher):
        """Test TelegramBot initialization."""
        with patch('backend.telegram_notifier.telegram_norifier.backend_settings') as mock_settings:
            mock_settings.get.return_value = "test_token"
            
            bot = TelegramBot()
            
            assert bot.token == "test_token"
            assert bot.bot is not None
            assert bot.dp is not None
            assert bot.logger is not None
    
    async def test_start_command(self, telegram_bot):
        """Test /start command handling."""
        from aiogram.types import Message
        
        # Mock message
        mock_message = MagicMock(spec=Message)
        mock_message.from_user = MagicMock()
        mock_message.from_user.id = 123456789
        mock_message.answer = AsyncMock()
        
        # Mock database operations
        with patch('backend.telegram_notifier.telegram_norifier.async_session_factory') as mock_session_factory:
            mock_session = AsyncMock()
            mock_session_factory.return_value.__aenter__.return_value = mock_session
            
            # Mock DAO
            with patch.object(TelegramUserDao, 'activate_user_or_create', new_callable=AsyncMock, return_value=True):
                with patch.object(TelegramUserDao, 'get_user_by_user_id', new_callable=AsyncMock, return_value=None):
                    with patch.object(TelegramUserDao, 'get_all_user_ids', new_callable=AsyncMock, return_value=[123456789, 987654321]):
                        # Execute start command
                        await telegram_bot.start(mock_message)

                        # Verify response
                        mock_message.answer.assert_called_with("Вы подписались на уведомления по ГРС!")
    
    async def test_start_command_already_subscribed(self, telegram_bot):
        """Test /start command when user is already subscribed."""
        from aiogram.types import Message
        
        # Mock message
        mock_message = MagicMock(spec=Message)
        mock_message.from_user = MagicMock()
        mock_message.from_user.id = 123456789
        mock_message.answer = AsyncMock()
        
        # Mock database operations
        with patch('backend.telegram_notifier.telegram_norifier.async_session_factory') as mock_session_factory:
            mock_session = AsyncMock()
            mock_session_factory.return_value.__aenter__.return_value = mock_session
            
            # Mock DAO
            with patch.object(TelegramUserDao, 'activate_user_or_create', new_callable=AsyncMock, return_value=False):
                with patch.object(TelegramUserDao, 'get_user_by_user_id', new_callable=AsyncMock, return_value=None):
                    with patch.object(TelegramUserDao, 'get_all_user_ids', new_callable=AsyncMock, return_value=[123456789, 987654321]):
                        # Execute start command
                        await telegram_bot.start(mock_message)

                        # Verify response
                        mock_message.answer.assert_called_with("Вы уже подписаны на обновления!")
    
    async def test_stop_command(self, telegram_bot):
        """Test /stop command handling."""
        from aiogram.types import Message
        
        # Mock message
        mock_message = MagicMock(spec=Message)
        mock_message.from_user = MagicMock()
        mock_message.from_user.id = 123456789
        mock_message.answer = AsyncMock()
        
        # Mock database operations
        with patch('backend.telegram_notifier.telegram_norifier.async_session_factory') as mock_session_factory:
            mock_session = AsyncMock()
            mock_session_factory.return_value.__aenter__.return_value = mock_session
            
            # Mock DAO
            with patch.object(TelegramUserDao, 'deactivate_user_by_user_id', new_callable=AsyncMock, return_value=True):
                with patch.object(TelegramUserDao, 'get_user_by_user_id', new_callable=AsyncMock, return_value=None):
                    with patch.object(TelegramUserDao, 'get_all_user_ids', new_callable=AsyncMock, return_value=[123456789, 987654321]):
                        # Execute stop command
                        await telegram_bot.stop(mock_message)

                        # Verify response
                        mock_message.answer.assert_called_with("Вы отписались от уведомлений.")
    
    async def test_stop_command_not_subscribed(self, telegram_bot):
        """Test /stop command when user is not subscribed."""
        from aiogram.types import Message
        
        # Mock message
        mock_message = MagicMock(spec=Message)
        mock_message.from_user = MagicMock()
        mock_message.from_user.id = 123456789
        mock_message.answer = AsyncMock()
        
        # Mock database operations
        with patch('backend.telegram_notifier.telegram_norifier.async_session_factory') as mock_session_factory:
            mock_session = AsyncMock()
            mock_session_factory.return_value.__aenter__.return_value = mock_session
            
            # Mock DAO
            with patch.object(TelegramUserDao, 'deactivate_user_by_user_id', new_callable=AsyncMock, return_value=False):
                with patch.object(TelegramUserDao, 'get_user_by_user_id', new_callable=AsyncMock, return_value=None):
                    with patch.object(TelegramUserDao, 'get_all_user_ids', new_callable=AsyncMock, return_value=[123456789, 987654321]):
                        # Execute stop command
                        await telegram_bot.stop(mock_message)

                        # Verify response
                        mock_message.answer.assert_called_with("Вы не были подписаны на уведомления!")
    
    async def test_unknown_command(self, telegram_bot):
        """Test unknown command handling."""
        from aiogram.types import Message
        
        # Mock message
        mock_message = MagicMock(spec=Message)
        mock_message.answer = AsyncMock()
        
        # Execute unknown command
        await telegram_bot.unknown_command(mock_message)
        
        # Verify response
        mock_message.answer.assert_called_with("Неизвестная команда. Используйте /start или /stop.")
    
    async def test_send_updates_success(self, telegram_bot):
        """Test sending updates to subscribers."""
        # Mock database operations
        with patch('backend.telegram_notifier.telegram_norifier.async_session_factory') as mock_session_factory:
            mock_session = AsyncMock()
            mock_session_factory.return_value.__aenter__.return_value = mock_session
            
            # Mock DAO
            with patch.object(TelegramUserDao, 'get_all_user_ids', new_callable=AsyncMock, return_value=[123456789, 987654321]) as mock_get_all:
                # Mock _send_message method
                telegram_bot._send_message = AsyncMock()

                # Execute send_updates
                await telegram_bot.send_updates("Test message")

                # Verify DAO was called
                mock_get_all.assert_called_once()
                
                # Verify _send_message was called for each user
                assert telegram_bot._send_message.call_count == 2
                telegram_bot._send_message.assert_any_call(123456789, "Test message")
                telegram_bot._send_message.assert_any_call(987654321, "Test message")
    
    async def test_send_message_success(self, telegram_bot):
        """Test sending individual message."""
        # Execute _send_message
        await telegram_bot._send_message(123456789, "Test message")
        
        # Verify bot.send_message was called
        telegram_bot.bot.send_message.assert_called_once_with(
            123456789, "Test message", parse_mode="HTML"
        )
    
    async def test_send_message_failure(self, telegram_bot):
        """Test sending message with failure."""
        # Mock bot.send_message to raise exception
        telegram_bot.bot.send_message.side_effect = Exception("Send failed")
        
        # Execute _send_message (should not raise exception)
        await telegram_bot._send_message(123456789, "Test message")
        
        # Verify bot.send_message was called
        telegram_bot.bot.send_message.assert_called_once()
    
    async def test_run_bot(self, telegram_bot):
        """Test running the bot."""
        # Execute run
        await telegram_bot.run()
        
        # Verify dispatcher.start_polling was called
        telegram_bot.dp.start_polling.assert_called_once_with(telegram_bot.bot)
    
    async def test_stop_bot(self, telegram_bot):
        """Test stopping the bot."""
        # Execute stop_bot
        await telegram_bot.stop_bot()
        
        # Verify cleanup methods were called
        telegram_bot.bot.session.close.assert_called_once()
        telegram_bot.bot.close.assert_called_once()
        telegram_bot.dp.stop_polling.assert_called_once()


class TestEmailNotifier:
    """Test email notifier functionality."""
    
    @pytest.fixture
    def email_notifier(self):
        """Create EmailNotifier instance."""
        with patch('backend.telegram_notifier.email_notifier.backend_settings') as mock_settings:
            mock_settings.get.side_effect = lambda key: {
                "SENDER_EMAIL": "test@example.com",
                "EMAIL_PASSWORD": "test_password",
                "EMAIL_RECEIVERS": ["receiver@example.com"]
            }.get(key)
            notifier = EmailNotifier()
            return notifier

    def test_email_notifier_initialization(self):
        """Test EmailNotifier initialization."""
        with patch('backend.telegram_notifier.email_notifier.backend_settings') as mock_settings:
            mock_settings.get.side_effect = lambda key: {
                "SENDER_EMAIL": "test@example.com",
                "EMAIL_PASSWORD": "test_password",
                "EMAIL_RECEIVERS": ["receiver@example.com"]
            }.get(key)

            notifier = EmailNotifier()

            assert notifier.sender_email == "test@example.com"
            assert notifier.sender_email_password == "test_password"
            assert notifier.email_receivers == ["receiver@example.com"]
    
    @patch('backend.telegram_notifier.email_notifier.smtplib.SMTP_SSL')
    def test_send_message_success(self, mock_smtp, email_notifier):
        """Test successful message sending."""
        # Mock SMTP
        mock_smtp_instance = MagicMock()
        mock_smtp.return_value.__enter__.return_value = mock_smtp_instance
        
        # Execute send_message
        email_notifier.send_message("Test message")
        
        # Verify SMTP was used
        mock_smtp.assert_called_once_with("smtp.gmail.com", 465)
        mock_smtp_instance.login.assert_called_once_with("test@example.com", "test_password")
        mock_smtp_instance.send_message.assert_called_once()


class TestHostlibUpdater:
    """Test HostlibUpdater functionality."""
    
    @pytest.fixture
    def hostlib_updater(self):
        """Create HostlibUpdater instance."""
        return HostlibUpdater()
    
    @patch('backend.hl_engine.hostlib_updater.RootRouter')
    async def test_update_hostlibs(self, mock_root_router_class, hostlib_updater):
        """Test update_hostlibs method."""
        # Mock RootRouter
        mock_root = AsyncMock()
        mock_root_router_class.return_value = mock_root
        
        # Execute update_hostlibs
        await hostlib_updater.update_hostlibs()
        
        # Verify RootRouter was called
        mock_root.update_data.assert_called_once()
    
    @patch('backend.hl_engine.hostlib_updater.TelegramBot')
    async def test_send_telegram_message(self, mock_telegram_bot_class, hostlib_updater):
        """Test send_telegram_message method."""
        # Mock TelegramBot
        mock_bot = AsyncMock()
        mock_telegram_bot_class.return_value = mock_bot
        
        # Execute send_telegram_message
        await hostlib_updater.send_telegram_message("Test message")
        
        # Verify TelegramBot was used
        mock_bot.send_updates.assert_called_once_with("Test message")
        mock_bot.bot.session.close.assert_called_once()
    
    def test_send_email_message(self, hostlib_updater):
        """Test sending email message."""
        with patch('backend.hl_engine.hostlib_updater.EmailNotifier') as mock_email_notifier_class:
            mock_notifier = MagicMock()
            mock_email_notifier_class.return_value = mock_notifier
            
            # Execute send_email_message
            hostlib_updater.send_email_message("Test message")
            
            # Verify EmailNotifier was used
            mock_notifier.send_message.assert_called_once_with(message="Test message")
    
    async def test_create_message(self, hostlib_updater):
        """Test create_message method."""
        # Create sample DataFrame
        data = {
            'line_id': [1, 1, 2, 2],
            'period': [
                datetime(2024, 12, 25, 14, 0),
                datetime(2024, 12, 25, 15, 0),
                datetime(2024, 12, 25, 14, 0),
                datetime(2024, 12, 25, 15, 0)
            ],
            'volume': [1000.0, 2000.0, 1500.0, 2500.0],
            'pressure': [5.0, 5.2, 4.8, 5.1],
            'w_volume_dp': [0.1, 0.2, 0.15, 0.25]
        }
        df = pd.DataFrame(data)
        
        # Mock database operations
        with patch('backend.hl_engine.hostlib_updater.async_session_factory') as mock_session_factory:
            mock_session = AsyncMock()
            mock_session_factory.return_value.__aenter__.return_value = mock_session
            
            # Mock LineDao
            mock_line_dao = AsyncMock()
            mock_line = Line(id=1, name="Test Line", meter=True)
            mock_line_dao.get_line_name_by_id.return_value = mock_line
            
            with patch('backend.hl_engine.hostlib_updater.LineDao') as mock_dao_class:
                mock_dao_class.return_value = mock_line_dao
                
                # Execute create_message
                message = await hostlib_updater.create_message(df)
                
                # Verify message structure
                assert "Объем по ГРС за последние 24 часа" in message
                assert "ГРС" in message
                assert "Test Line" in message
                assert "3 000.0" in message
    
    async def test_create_email_message(self, hostlib_updater):
        """Test create_email_message method."""
        # Create sample DataFrame
        data = {
            'line_id': [1, 1, 2, 2],
            'period': [
                datetime(2024, 12, 25, 14, 0),
                datetime(2024, 12, 25, 15, 0),
                datetime(2024, 12, 25, 14, 0),
                datetime(2024, 12, 25, 15, 0)
            ],
            'volume': [1000.0, 2000.0, 1500.0, 2500.0],
            'pressure': [5.0, 5.2, 4.8, 5.1],
            'w_volume_dp': [0.1, 0.2, 0.15, 0.25]
        }
        df = pd.DataFrame(data)
        
        # Mock database operations
        with patch('backend.hl_engine.hostlib_updater.async_session_factory') as mock_session_factory:
            mock_session = AsyncMock()
            mock_session_factory.return_value.__aenter__.return_value = mock_session
            
            # Mock LineDao
            mock_line_dao = AsyncMock()
            mock_line = Line(id=1, name="Test Line", meter=True)
            mock_line_dao.get_line_name_by_id.return_value = mock_line
            
            with patch('backend.hl_engine.hostlib_updater.LineDao') as mock_dao_class:
                mock_dao_class.return_value = mock_line_dao
                
                # Execute create_email_message
                message = await hostlib_updater.create_email_message(df)
                
                # Verify message structure
                assert "<html>" in message
                assert "<body>" in message
                assert "<h1>Объем по ГРС за последние 24 часа</h1>" in message
                assert "<table" in message
                assert "Test Line" in message
    
    @patch('backend.hl_engine.hostlib_updater.HostlibUpdater.update_hostlibs')
    @patch('backend.hl_engine.hostlib_updater.HostlibUpdater.send_telegram_message')
    @patch('backend.hl_engine.hostlib_updater.HourlyArchiveDao')
    async def test_update_and_send_notification(
        self, 
        mock_dao_class, 
        mock_send_telegram, 
        mock_update_hostlibs, 
        hostlib_updater
    ):
        """Test update_and_send_notification method."""
        # Mock DAO
        mock_dao = AsyncMock()
        mock_dao_class.return_value = mock_dao
        
        # Mock data
        mock_archives = [
            HourlyArchive(
                id=1, period=datetime(2024, 12, 25, 14, 0),
                volume=1000.0, line_id=1, pressure=5.0, w_volume_dp=0.1,
                temperature=20.0, density=0.7
            ),
            HourlyArchive(
                id=2, period=datetime(2024, 12, 25, 15, 0),
                volume=2000.0, line_id=1, pressure=5.2, w_volume_dp=0.2,
                temperature=21.0, density=0.7
            )
        ]
        mock_dao.get_last_period.return_value = datetime(2024, 12, 25, 15, 0)
        mock_dao.get_range.return_value = mock_archives
        
        # Mock session factory
        with patch('backend.hl_engine.hostlib_updater.async_session_factory') as mock_session_factory:

            mock_session = AsyncMock()
            mock_session_factory.return_value.__aenter__.return_value = mock_session

            # Mock LineDao
            with patch('backend.hl_engine.hostlib_updater.LineDao') as mock_line_dao_class:
                mock_line_dao_instance = MagicMock()
                mock_line = Line(id=1, name="Test Line", meter=True)
                mock_line_dao_instance.get_line_name_by_id = AsyncMock(return_value=mock_line)
                mock_line_dao_class.return_value = mock_line_dao_instance

                # Execute update_and_send_notification
                await hostlib_updater.update_and_send_notification()
            
            # Verify methods were called
            mock_update_hostlibs.assert_called_once()
            mock_send_telegram.assert_called_once()
            mock_dao.get_last_period.assert_called_once()
            mock_dao.get_range.assert_called_once() 